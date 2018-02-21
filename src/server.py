#!/usr/bin/env python3
import json
import logging
import logging.config
import os
import ssl
import threading
import time
from datetime import datetime
from urllib.parse import urlencode
from urllib.parse import urljoin
from urllib.parse import urlparse

import tornado.concurrent
import tornado.escape
import tornado.httpserver as httpserver
import tornado.ioloop
import tornado.web
import tornado.websocket
from tornado import gen

from auth import auth_base
from execution.executor import ScriptExecutor
from execution.logging import ScriptOutputLogger
from features import file_download_feature
from model import external_model
from model import model_helper
from model import script_configs
from model import server_conf
from react.observable import Observable
from utils import bash_utils as bash_utils
from utils import file_utils as file_utils
from utils import os_utils as os_utils
from utils import tool_utils
from utils.audit_utils import get_all_audit_names
from utils.audit_utils import get_audit_name

TEMP_FOLDER = "temp"

CONFIG_FOLDER = "conf"
SERVER_CONF_PATH = os.path.join(CONFIG_FOLDER, "conf.json")
SCRIPT_CONFIGS_FOLDER = os.path.join(CONFIG_FOLDER, "runners")
LOGGER = logging.getLogger('script_server')

running_scripts = {}


def list_config_names():
    def add_name(path, content):
        try:
            return script_configs.read_name(path, content)

        except:
            LOGGER.exception('Could not load script name: ' + path)

    result = visit_script_configs(add_name)

    return result


def load_config(name):
    def find_and_load(path, content):
        try:
            config_name = script_configs.read_name(path, content)
            if config_name == name:
                return script_configs.from_json(path, content, os_utils.is_pty_supported())
        except:
            LOGGER.exception('Could not load script config: ' + path)

    configs = visit_script_configs(find_and_load)
    if configs:
        return configs[0]

    return None


def visit_script_configs(visitor):
    configs_dir = SCRIPT_CONFIGS_FOLDER
    files = os.listdir(configs_dir)

    configs = [file for file in files if file.lower().endswith(".json")]

    result = []

    for config_path in configs:
        path = os.path.join(configs_dir, config_path)

        try:
            content = file_utils.read_file(path)

            visit_result = visitor(path, content)
            if visit_result is not None:
                result.append(visit_result)

        except:
            LOGGER.exception("Couldn't read the file: " + config_path)

    return result


class TornadoAuth():
    authorizer = None

    def __init__(self, authorizer):
        self.authorizer = authorizer

    def is_enabled(self):
        return bool(self.authorizer)

    def is_authenticated(self, request_handler):
        if not self.is_enabled():
            return True

        username = request_handler.get_secure_cookie("username")

        return bool(username)

    def get_username(self, request_handler):
        if not self.is_enabled():
            return None

        username = request_handler.get_secure_cookie("username")
        if not username:
            return None

        return username.decode("utf-8")

    @gen.coroutine
    def authenticate(self, request_handler):
        if not self.is_enabled():
            return

        LOGGER.info('Trying to authenticate user')

        login_generic_error = 'Something went wrong. Please contact the administrator or try later'

        try:
            username = self.authorizer.authenticate(request_handler)
            if isinstance(username, tornado.concurrent.Future):
                username = yield username

        except auth_base.AuthRejectedError as e:
            respond_error(request_handler, 401, e.get_message())
            return

        except auth_base.AuthFailureError:
            respond_error(request_handler, 500, login_generic_error)
            return

        except auth_base.AuthRedirectedException as e:
            redirect(e.redirect_url, request_handler)
            return
        except:
            LOGGER.exception('Failed to call authenticate')
            respond_error(request_handler, 500, login_generic_error)
            return

        LOGGER.info('Authenticated user ' + username)

        request_handler.set_secure_cookie('username', username)

        path = tornado.escape.url_unescape(request_handler.get_argument('next', '/'))
        if path.startswith('http'):
            path = '/'

        url_fragment = request_handler.get_argument('url_fragment', '')
        if url_fragment:
            path += '#' + tornado.escape.url_unescape(url_fragment)

        redirect_relative(path, request_handler)

    def logout(self, request_handler):
        if not self.is_enabled():
            return

        username = self.get_username(request_handler)
        if not username:
            return

        LOGGER.info('Logging out ' + username)

        request_handler.clear_cookie("username")


def redirect_relative(relative_url, request_handler, *args, **kwargs):
    full_url = get_full_url(relative_url, request_handler)
    redirect(full_url, request_handler, *args, **kwargs)


def is_ajax(request):
    requested_with = request.headers.get('X-Requested-With')
    return requested_with == 'XMLHttpRequest'


def redirect(full_url, request_handler, *args, **kwargs):
    if is_ajax(request_handler.request):
        # For AJAX we need custom handling because of:
        #   1. browsers ignore url fragments (#hash) of redirects inside ajax
        #   2. redirecting ajax to external resources causes allowed origin header issues
        request_handler.set_header('Location', full_url)
        request_handler.set_status(200)
    else:
        request_handler.redirect(full_url, *args, **kwargs)


def get_full_url(relative_url, request_handler):
    request = request_handler.request
    host_url = request.protocol + "://" + request.host
    return urljoin(host_url, relative_url)


def is_allowed_during_login(request_path, login_url, request_handler):
    if request_handler.request.method == 'POST':
        if request_path == '/login':
            return True

    elif request_handler.request.method == 'GET':
        if request_path == '/favicon.ico':
            return True

        if (request_path == login_url) or (request_path == '/auth/type'):
            return True

        login_resources = ['/js/login.js',
                           '/js/common.js',
                           '/js/libs/jquery.min.js',
                           '/js/libs/materialize.min.js',
                           '/css/libs/materialize.min.css',
                           '/css/index.css',
                           '/css/fonts/roboto/Roboto-Regular.woff2',
                           '/css/fonts/roboto/Roboto-Regular.woff',
                           '/css/fonts/roboto/Roboto-Regular.ttf',
                           '/images/titleBackground.jpg',
                           '/images/g-logo-plain.png',
                           '/images/g-logo-plain-pressed.png']

        if request_path not in login_resources:
            return False

        referer = request_handler.request.headers.get('Referer')
        if referer:
            referer = urlparse(referer).path
        else:
            return False

        allowed_referrers = [login_url, '/css/libs/materialize.min.css', '/css/index.css']
        for allowed_referrer in allowed_referrers:
            if referer.endswith(allowed_referrer):
                return True

    return False


# This decorator is used for REST requests, in which we don't redirect explicitly, but reply with Unauthorized code.
# Client application should provide redirection in the way it likes
def check_authorization(func):
    def wrapper(self, *args, **kwargs):
        auth = self.application.auth
        request_path = self.request.path
        login_url = self.get_login_url()

        if auth.is_authenticated(self) or is_allowed_during_login(request_path, login_url, self):
            return func(self, *args, **kwargs)

        if not isinstance(self, tornado.web.StaticFileHandler):
            raise tornado.web.HTTPError(401, "Unauthorized")

        login_url += "?" + urlencode(dict(next=request_path))

        redirect_relative(login_url, self)

        return

    return wrapper


class ProxiedRedirectHandler(tornado.web.RedirectHandler):
    def get(self, *args):
        redirect_relative(self._url.format(*args), self, *args)


class BaseRequestHandler(tornado.web.RequestHandler):
    def set_default_headers(self):
        self.set_header('X-Frame-Options', 'DENY')


class BaseStaticHandler(tornado.web.StaticFileHandler):
    def set_default_headers(self):
        self.set_header('X-Frame-Options', 'DENY')


class GetServerTitle(BaseRequestHandler):
    def get(self):
        if self.application.server_title:
            self.write(self.application.server_title)


class GetScripts(BaseRequestHandler):
    @check_authorization
    def get(self):
        config_names = list_config_names()

        self.write(json.dumps(config_names))


class GetScriptInfo(BaseRequestHandler):
    @check_authorization
    def get(self):
        try:
            name = self.get_query_argument("name")
        except tornado.web.MissingArgumentError:
            respond_error(self, 400, "Script name is not specified")
            return

        config = load_config(name)

        if not config:
            respond_error(self, 400, "Couldn't find a script by name")
            return

        self.write(external_model.config_to_json(config))


def stop_script(process_id):
    if process_id in running_scripts:
        running_scripts[process_id].stop()


class ScriptStop(BaseRequestHandler):
    @check_authorization
    def post(self):
        request_body = self.request.body.decode("UTF-8")
        process_id = json.loads(request_body).get("processId")

        if (process_id):
            stop_script(int(process_id))
        else:
            respond_error(self, 400, "Invalid stop request")
            return


class ScriptStreamSocket(tornado.websocket.WebSocketHandler):
    def __init__(self, application, request, **kwargs):
        super().__init__(application, request, **kwargs)

        self.executor = None

    def open(self, process_id):
        auth = self.application.auth
        if not auth.is_authenticated(self):
            return None

        executor = running_scripts.get(int(process_id))  # type: ScriptExecutor

        if not executor:
            raise Exception("Couldn't find corresponding process")

        self.executor = executor

        self.write_message(wrap_to_server_event("input", "your input >>"))

        self.write_message(wrap_script_output(" ---  OUTPUT  --- \n"))

        audit_name = get_audit_name(self)

        output_stream = executor.get_unsecure_output_stream()
        bash_formatting = executor.config.is_bash_formatting()
        pipe_output_to_http(output_stream, bash_formatting, self.safe_write)

        web_socket = self

        class FinishListener(object):
            def finished(self):
                output_stream.wait_close()
                script_output = ''.join(output_stream.get_old_data())

                try:
                    downloadable_files = file_download_feature.prepare_downloadable_files(
                        executor.config,
                        script_output,
                        executor.parameter_values,
                        audit_name,
                        get_tornado_secret(),
                        TEMP_FOLDER)

                    for file in downloadable_files:
                        filename = os.path.basename(file)
                        relative_path = file_utils.relative_path(file, TEMP_FOLDER)

                        web_socket.safe_write(wrap_to_server_event(
                            'file',
                            {'url': relative_path.replace(os.path.sep, '/'), 'filename': filename}))
                except:
                    LOGGER.exception("Couldn't prepare downloadable files")

                tornado.ioloop.IOLoop.current().add_callback(web_socket.close)

        executor.add_finish_listener(FinishListener())

    def on_message(self, text):
        self.executor.write_to_input(text)

    def on_close(self):
        self.executor.kill()

    def safe_write(self, message):
        if self.ws_connection is not None:
            self.write_message(message)


class ScriptExecute(BaseRequestHandler):
    @check_authorization
    def post(self):
        script_name = None

        audit_name = get_audit_name(self)

        try:
            request_data = self.request.body

            execution_info = external_model.to_execution_info(request_data.decode("UTF-8"))

            script_name = execution_info.script

            config = load_config(script_name)

            if not config:
                message = 'Script with name "' + str(script_name) + '" not found'
                LOGGER.error(message)
                respond_error(self, 400, message)
                return

            valid_parameters = model_helper.validate_parameters(execution_info.param_values, config)
            if not valid_parameters:
                message = 'Received invalid parameters'
                LOGGER.error(message)
                respond_error(self, 400, message)
                return

            executor = ScriptExecutor(config, execution_info.param_values, audit_name)

            audit_command = executor.get_secure_command()
            LOGGER.info('Calling script: ' + audit_command)
            LOGGER.info('User info: ' + str(get_all_audit_names(self)))

            process_id = executor.start()
            running_scripts[process_id] = executor

            self.write(str(process_id))

            secure_output_stream = executor.get_secure_output_stream()

            self.start_script_output_logger(audit_name, script_name, secure_output_stream)

            alerts_config = self.application.alerts_config
            if alerts_config:
                self.subscribe_fail_alerter(
                    script_name,
                    alerts_config,
                    audit_name,
                    executor,
                    secure_output_stream)


        except Exception as e:
            LOGGER.exception("Error while calling the script")

            if hasattr(e, "strerror") and e.strerror:
                error_output = e.strerror
            else:
                error_output = "Unknown error occurred, contact the administrator"

            result = " ---  ERRORS  --- \n"
            result += error_output

            if script_name:
                script = str(script_name)
            else:
                script = "Some script"

            audit_name = audit_name
            send_alerts(self.application.alerts_config,
                        script + ' NOT STARTED',
                        "Couldn't start the script " + script + ' by ' + audit_name + '.\n\n'
                        + result)

            respond_error(self, 500, result)

    def subscribe_fail_alerter(
            self,
            script_name,
            alerts_config,
            audit_name,
            executor: ScriptExecutor,
            output_stream: Observable):

        class Alerter(object):
            def finished(self):
                return_code = executor.get_return_code()

                if return_code != 0:
                    script = str(script_name)

                    title = script + ' FAILED'
                    body = 'The script "' + script + '", started by ' + audit_name + \
                           ' exited with return code ' + str(return_code) + '.' + \
                           ' Usually this means an error, occurred during the execution.' + \
                           ' Please check the corresponding logs'

                    output_stream.wait_close()
                    script_output = ''.join(output_stream.get_old_data())

                    send_alerts(alerts_config, title, body, script_output)

        executor.add_finish_listener(Alerter())

    def start_script_output_logger(self, audit_name, script_name, output_stream):
        log_identifier = self.create_log_identifier(audit_name, script_name)
        log_file_path = os.path.join('logs', 'processes', log_identifier + '.log')

        output_logger = ScriptOutputLogger(log_file_path, output_stream)
        output_logger.start()

    @staticmethod
    def create_log_identifier(audit_name, script_name):
        if os_utils.is_win():
            audit_name = audit_name.replace(":", "-")

        date_string = datetime.today().strftime("%y%m%d_%H%M%S")
        script_name = script_name.replace(" ", "_")
        log_identifier = script_name + "_" + audit_name + "_" + date_string
        return log_identifier


def send_alerts(alerts_config, title, body, logs=None):
    if (not alerts_config) or (not alerts_config.get_destinations()):
        return

    def _send():
        for destination in alerts_config.get_destinations():
            try:
                destination.send(title, body, logs)
            except:
                LOGGER.exception("Couldn't send alert to " + str(destination))

    thread = threading.Thread(target=_send)
    thread.start()


class AuthorizedStaticFileHandler(BaseStaticHandler):
    @check_authorization
    def validate_absolute_path(self, root, absolute_path):
        if not self.application.auth.is_enabled() and (absolute_path.endswith("/login.html")):
            raise tornado.web.HTTPError(404)

        return super(AuthorizedStaticFileHandler, self).validate_absolute_path(root, absolute_path)


class LoginHandler(BaseRequestHandler):
    def post(self):
        return self._authenticate()

    # GET is needed for oauth redirects
    def get(self):
        return self._authenticate()

    def _authenticate(self):
        auth = self.application.auth
        return auth.authenticate(self)


class AuthTypeHandler(BaseRequestHandler):
    def get(self):
        auth = self.application.auth
        if not auth.is_enabled():
            raise tornado.web.HTTPError(404)

        self.write(auth.authorizer.auth_type)


class LogoutHandler(BaseRequestHandler):
    @check_authorization
    def post(self):
        auth = self.application.auth

        auth.logout(self)


class GetUsernameHandler(BaseRequestHandler):
    @check_authorization
    def get(self):
        auth = self.application.auth
        if not auth.is_enabled():
            raise tornado.web.HTTPError(404)

        username = auth.get_username(self)
        self.write(username)


class DownloadResultFile(AuthorizedStaticFileHandler):
    def set_extra_headers(self, path):
        super().set_extra_headers(path)

        filename = os.path.basename(path)
        self.set_header('Content-Disposition', 'attachment; filename=' + filename + '')

    @check_authorization
    def validate_absolute_path(self, root, absolute_path):
        audit_name = get_audit_name(self)

        file_path = file_utils.relative_path(absolute_path, os.path.abspath(root))
        if not file_download_feature.allowed_to_download(file_path, audit_name, get_tornado_secret()):
            LOGGER.warning('Access attempt from ' + audit_name + ' to ' + absolute_path)
            raise tornado.web.HTTPError(404)

        return super(AuthorizedStaticFileHandler, self).validate_absolute_path(root, absolute_path)


# Use for testing only
class ReceiveAlertHandler(BaseRequestHandler):
    def post(self):
        message = self.get_body_argument('message')
        LOGGER.info('ReceiveAlertHandler. Received alert: ' + message)

        log_files = self.request.files['log']
        if log_files:
            file = log_files[0]
            filename = str(time.time()) + '_' + file.filename

            LOGGER.info('ReceiveAlertHandler. Writing file ' + filename)

            file_path = os.path.join('logs', 'alerts', filename)
            file_utils.write_file(file_path, file.body.decode('utf-8'))


def respond_error(request_handler, status_code, message):
    request_handler.set_status(status_code)
    request_handler.write(message)


def wrap_script_output(text, text_color=None, background_color=None, text_styles=None):
    output_object = {'text': text}

    if text_color:
        output_object['text_color'] = text_color

    if background_color:
        output_object['background_color'] = background_color

    if text_styles:
        output_object['text_styles'] = text_styles

    return wrap_to_server_event("output", output_object)


def wrap_to_server_event(event_type, data):
    return json.dumps({
        "event": event_type,
        "data": data
    })


def pipe_output_to_http(output_stream, bash_formatting, write_callback):
    reader = None
    if bash_formatting:
        reader = bash_utils.BashReader()

    class OutputToHttpListener:
        def on_next(self, output):
            if reader:
                read_iterator = reader.read(output)
                for text in read_iterator:
                    write_callback(wrap_script_output(
                        text.text,
                        text.text_color,
                        text.background_color,
                        text.styles
                    ))

            else:
                write_callback(wrap_script_output(output))

        def on_close(self):
            pass

    output_stream.subscribe(OutputToHttpListener())


def get_tornado_secret():
    secret_file = os.path.join("temp", "secret.dat")
    if os.path.exists(secret_file):
        secret = file_utils.read_file(secret_file, byte_content=True)
        if secret:
            return secret

    secret = os.urandom(256)
    file_utils.write_file(secret_file, secret, byte_content=True)
    return secret


def main():
    tool_utils.validate_web_imports_exist(os.getcwd())

    logging_conf_file = os.path.join(CONFIG_FOLDER, 'logging.json')
    with open(logging_conf_file, "rt") as f:
        log_config = json.load(f)
        file_utils.prepare_folder(os.path.join("logs", "processes"))

        logging.config.dictConfig(log_config)

    file_utils.prepare_folder(CONFIG_FOLDER)
    file_utils.prepare_folder(SCRIPT_CONFIGS_FOLDER)

    server_config = server_conf.from_json(SERVER_CONF_PATH)
    ssl_context = None
    if server_config.is_ssl():
        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_context.load_cert_chain(server_config.get_ssl_cert_path(),
                                    server_config.get_ssl_key_path())

    file_utils.prepare_folder(TEMP_FOLDER)

    settings = {
        "cookie_secret": get_tornado_secret(),
        "login_url": "/login.html"
    }

    auth = TornadoAuth(server_config.authorizer)

    result_files_folder = file_download_feature.get_result_files_folder(TEMP_FOLDER)
    file_download_feature.autoclean_downloads(TEMP_FOLDER)

    handlers = [(r"/conf/title", GetServerTitle),
                (r"/scripts/list", GetScripts),
                (r"/scripts/info", GetScriptInfo),
                (r"/scripts/execute", ScriptExecute),
                (r"/scripts/execute/stop", ScriptStop),
                (r"/scripts/execute/io/(.*)", ScriptStreamSocket),
                (r'/' + file_download_feature.RESULT_FILES_FOLDER + '/(.*)',
                 DownloadResultFile,
                 {'path': result_files_folder}),
                (r"/", ProxiedRedirectHandler, {"url": "/index.html"})]

    if auth.is_enabled():
        handlers.append((r'/login', LoginHandler))
        handlers.append((r'/auth/type', AuthTypeHandler))
        handlers.append((r'/logout', LogoutHandler))

    handlers.append((r"/username", GetUsernameHandler))

    handlers.append((r"/(.*)", AuthorizedStaticFileHandler, {"path": "web"}))

    application = tornado.web.Application(handlers, **settings)

    application.auth = auth

    application.alerts_config = server_config.get_alerts_config()
    application.server_title = server_config.title

    http_server = httpserver.HTTPServer(application, ssl_options=ssl_context)
    http_server.listen(server_config.port, address=server_config.address)
    tornado.ioloop.IOLoop.current().start()


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    main()
