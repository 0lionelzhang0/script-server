import json
import logging
import os

from model import script_configs, model_helper
from utils import os_utils, file_utils

LOGGER = logging.getLogger('config_service')


class ConfigService:
    def __init__(self, authorizer, conf_folder) -> None:
        self._authorizer = authorizer
        self._script_configs_folder = os.path.join(conf_folder, 'runners')
        self._cached_configs = {}

        file_utils.prepare_folder(self._script_configs_folder)

    def list_configs(self, user):
        conf_service = self

        def load_script(path, content):
            try:
                json_object = json.loads(content)
                short_config = script_configs.read_short(path, json_object)

                if not conf_service._can_access_script(user, short_config):
                    return None

                return short_config
            except:
                LOGGER.exception('Could not load script: ' + path)

        return self._visit_script_configs(load_script)

    def load_config(self, name, user):
        def find_and_load(path, content):
            try:
                json_object = json.loads(content)
                short_config = script_configs.read_short(path, json_object)
            except:
                LOGGER.exception('Could not load script config: ' + path)
                return None

            if short_config.name != name:
                return None

            raise StopIteration((short_config, path, json_object))

        configs = self._visit_script_configs(find_and_load)
        if not configs:
            return None

        (short_config, path, json_object) = configs[0]

        if not self._can_access_script(user, short_config):
            raise ConfigNotAllowedException()

        return self._load_script_config(path, json_object, user)

    def _visit_script_configs(self, visitor):
        configs_dir = self._script_configs_folder
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

            except StopIteration as e:
                if e.value is not None:
                    result.append(e.value)

            except:
                LOGGER.exception("Couldn't read the file: " + config_path)

        return result

    def get_parameter_values(self, script_name, param_name, current_values, user):
        script_config = self._get_cached_config(script_name, user)
        if script_config is None:
            raise ConfigNotFoundException(script_name)

        found_parameter = None
        for parameter in script_config.parameters:
            if parameter.name == param_name:
                found_parameter = parameter
                break

        if found_parameter is None:
            raise ParameterNotFoundException(param_name, script_name)

        required_parameters = found_parameter.get_required_parameters()
        if not required_parameters:
            return found_parameter.get_values([])

        for required_parameter_name in required_parameters:
            required_parameter = script_config.find_parameter(required_parameter_name)
            if required_parameter is None:
                raise Exception('Failed to find required parameter ' + required_parameter_name
                                + ' for ' + script_name)

            validation_error = model_helper.validate_parameter(required_parameter, current_values)
            if validation_error is not None:
                raise InvalidValueException(param_name, validation_error, script_name)

        return found_parameter.get_values(current_values)

    def _get_cached_config(self, script_name, user):
        if script_name in self._cached_configs:
            return self._cached_configs[script_name]

        return self.load_config(script_name, user)

    def _load_script_config(self, path, content_or_json_dict, user):
        if isinstance(content_or_json_dict, str):
            json_object = json.loads(content_or_json_dict)
        else:
            json_object = content_or_json_dict
        config = script_configs.read_full(
            path,
            json_object,
            user.get_username(),
            user.get_audit_name(),
            os_utils.is_pty_supported())

        self._cached_configs[config.name] = config

        return config

    def _can_access_script(self, user, short_config):
        return self._authorizer.is_allowed(user.user_id, short_config.allowed_users)


class ConfigNotFoundException(Exception):
    def __init__(self, script_name) -> None:
        self.script_name = script_name


class ParameterNotFoundException(Exception):
    def __init__(self, param_name, script_name) -> None:
        self.param_name = param_name
        self.script_name = script_name


class InvalidValueException(Exception):
    def __init__(self, param_name, validation_error, script_name) -> None:
        self.param_name = param_name
        self.validation_error = validation_error
        self.script_name = script_name


class ConfigNotAllowedException(Exception):
    def __init__(self):
        pass
