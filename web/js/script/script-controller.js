function ScriptController(scriptConfig, scriptName, executionStartCallback) {
    this.scriptConfig = scriptConfig;
    this.scriptName = scriptName;
    this.executionStartCallback = executionStartCallback;
    this.scriptView = null;

    this.logPublisher = null;
    this.logLastIndex = 0;
    this.executor = null;
    this.executorListener = null;
}

ScriptController.prototype.fillView = function (parent) {
    var scriptView = new ScriptView(parent);
    this.scriptView = scriptView;

    scriptView.setScriptDescription(this.scriptConfig.description);
    scriptView.createParameters(this.scriptConfig.parameters);

    scriptView.executeButtonCallback = function () {
        scriptView.setLog('Calling the script...');

        var parameterValues = new Hashtable();
        scriptView.parameterControls.each(function (parameter, control) {
            parameterValues.put(parameter.name, control.getValue());
        });

        try {
            this.executor = new ScriptExecutor(this.scriptConfig, this.scriptName);
            this.executor.start(parameterValues);
            this.executionStartCallback(this.executor);

            this._updateViewWithExecutor(this.executor);

        } catch (error) {
            if (!(error instanceof HttpRequestError) || (error.code !== 401)) {
                logError(error);

                scriptView.setLog(error.message);
            }
        }
    }.bind(this);

    scriptView.stopButtonCallback = function () {
        if (!isNull(this.executor)) {
            this.executor.stop();
        }
    }.bind(this);

    return scriptView.scriptPanel;
};

ScriptController.prototype.destroy = function () {
    if (!isNull(this.scriptView)) {
        this.scriptView.destroy();
    }
    this.scriptView = null;

    this._stopLogPublisher();

    if (!isNull(this.executor)) {
        this.executor.removeListener(this.executorListener);
    }
};

ScriptController.prototype.setExecutor = function (executor) {
    this.executor = executor;

    this.scriptView.setParameterValues(executor.parameterValues);

    this._updateViewWithExecutor(executor);
};

ScriptController.prototype._updateViewWithExecutor = function (executor) {
    this.scriptView.setExecuting();

    this._startLogPublisher();

    this.executorListener = {
        'onExecutionStop': function () {
            this._publishLogs();
            this._stopLogPublisher();

            this.scriptView.setStopEnabled(false);
            this.scriptView.setExecutionEnabled(true);

            this.scriptView.hideInputField();
        }.bind(this),

        'onInputPrompt': function (promptText) {
            this.scriptView.showInputField(promptText, function (inputText) {
                executor.sendUserInput(inputText);
            });
        }.bind(this),

        'onFileCreated': function (url, filename) {
            this.scriptView.addFileLink(url, filename);
        }.bind(this)
    };

    executor.addListener(this.executorListener);
};

ScriptController.prototype._startLogPublisher = function () {
    this._stopLogPublisher();

    this.logLastIndex = 0;

    this._publishLogs();
    this.logPublisher = window.setInterval(this._publishLogs.bind(this), 30);
};

ScriptController.prototype._stopLogPublisher = function () {
    if (!isNull(this.logPublisher)) {
        window.clearInterval(this.logPublisher);
        this.logPublisher = null;
    }
};

ScriptController.prototype._publishLogs = function () {
    if (isNull(this.scriptView)) {
        return;
    }

    var logElements = this.executor.logElements;

    if ((this.logLastIndex === 0) && (logElements.length > 0)) {
        this.scriptView.setLog('');
    }

    for (; this.logLastIndex < logElements.length; this.logLastIndex++) {
        var logIndex = this.logLastIndex;
        this.scriptView.appendLog(logElements[logIndex]);
    }
};
