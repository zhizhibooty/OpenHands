import os
import tempfile
import threading
from typing import Callable, Optional
from zipfile import ZipFile

import requests
import tenacity

from openhands.core.config import AppConfig
from openhands.events import EventStream
from openhands.events.action import (
    BrowseInteractiveAction,
    BrowseURLAction,
    CmdRunAction,
    FileEditAction,
    FileReadAction,
    FileWriteAction,
    IPythonRunCellAction,
)
from openhands.events.action.action import Action
from openhands.events.observation import (
    ErrorObservation,
    NullObservation,
    Observation,
)
from openhands.events.serialization import event_to_dict, observation_from_dict
from openhands.events.serialization.action import ACTION_TYPE_TO_CLASS
from openhands.runtime.base import (
    Runtime,
    RuntimeDisconnectedError,
    RuntimeNotReadyError,
)
from openhands.runtime.builder.remote import RemoteRuntimeBuilder
from openhands.runtime.plugins import PluginRequirement
from openhands.runtime.utils.command import get_remote_startup_command
from openhands.runtime.utils.request import (
    send_request,
)
from openhands.runtime.utils.runtime_build import build_runtime_image
from openhands.utils.async_utils import call_sync_from_async
from openhands.utils.tenacity_stop import stop_if_should_exit


class RemoteRuntime(Runtime):
    """This runtime will connect to a remote oh-runtime-client."""

    port: int = 60000  # default port for the remote runtime client

    def __init__(
        self,
        config: AppConfig,
        event_stream: EventStream,
        sid: str = 'default',
        plugins: list[PluginRequirement] | None = None,
        env_vars: dict[str, str] | None = None,
        status_callback: Optional[Callable] = None,
        attach_to_existing: bool = False,
    ):
        # We need to set session and action_semaphore before the __init__ below, or we get odd errors
        self.session = requests.Session()
        self.action_semaphore = threading.Semaphore(1)

        super().__init__(
            config,
            event_stream,
            sid,
            plugins,
            env_vars,
            status_callback,
            attach_to_existing,
        )
        if self.config.sandbox.api_key is None:
            raise ValueError(
                'API key is required to use the remote runtime. '
                'Please set the API key in the config (config.toml) or as an environment variable (SANDBOX_API_KEY).'
            )
        self.session.headers.update({'X-API-Key': self.config.sandbox.api_key})

        if self.config.workspace_base is not None:
            self.log(
                'debug',
                'Setting workspace_base is not supported in the remote runtime.',
            )

        self.runtime_builder = RemoteRuntimeBuilder(
            self.config.sandbox.remote_runtime_api_url, self.config.sandbox.api_key
        )
        self.runtime_id: str | None = None
        self.runtime_url: str | None = None

    async def connect(self):
        await call_sync_from_async(self._start_or_attach_to_runtime)
        try:
            await call_sync_from_async(self._wait_until_alive)
        except RuntimeNotReadyError:
            self.log('error', 'Runtime failed to start, timed out before ready')
            raise
        await call_sync_from_async(self.setup_initial_env)

    def _start_or_attach_to_runtime(self):
        existing_runtime = self._check_existing_runtime()
        if existing_runtime:
            self.log('debug', f'Using existing runtime with ID: {self.runtime_id}')
        elif self.attach_to_existing:
            raise RuntimeError('Could not find existing runtime to attach to.')
        else:
            self.send_status_message('STATUS$STARTING_CONTAINER')
            if self.config.sandbox.runtime_container_image is None:
                self.log(
                    'info',
                    f'Building remote runtime with base image: {self.config.sandbox.base_container_image}',
                )
                self._build_runtime()
            else:
                self.log(
                    'info',
                    f'Starting remote runtime with image: {self.config.sandbox.runtime_container_image}',
                )
                self.container_image = self.config.sandbox.runtime_container_image
            self._start_runtime()
        assert (
            self.runtime_id is not None
        ), 'Runtime ID is not set. This should never happen.'
        assert (
            self.runtime_url is not None
        ), 'Runtime URL is not set. This should never happen.'
        self.send_status_message('STATUS$WAITING_FOR_CLIENT')
        if not self.attach_to_existing:
            self.log('info', 'Waiting for runtime to be alive...')
        self._wait_until_alive()
        if not self.attach_to_existing:
            self.log('info', 'Runtime is ready.')
        self.send_status_message(' ')

    def _check_existing_runtime(self) -> bool:
        try:
            response = self._send_request(
                'GET',
                f'{self.config.sandbox.remote_runtime_api_url}/runtime/{self.sid}',
                timeout=5,
            )
        except requests.HTTPError as e:
            if e.response.status_code == 404:
                return False
            self.log('debug', f'Error while looking for remote runtime: {e}')
            raise

        data = response.json()
        status = data.get('status')
        if status == 'running':
            self._parse_runtime_response(response)
            return True
        elif status == 'stopped':
            self.log('debug', 'Found existing remote runtime, but it is stopped')
            return False
        elif status == 'paused':
            self.log('debug', 'Found existing remote runtime, but it is paused')
            self._parse_runtime_response(response)
            self._resume_runtime()
            return True
        else:
            self.log('error', f'Invalid response from runtime API: {data}')
            return False

    def _build_runtime(self):
        self.log('debug', f'Building RemoteRuntime config:\n{self.config}')
        response = self._send_request(
            'GET',
            f'{self.config.sandbox.remote_runtime_api_url}/registry_prefix',
            timeout=10,
        )
        response_json = response.json()
        registry_prefix = response_json['registry_prefix']
        os.environ['OH_RUNTIME_RUNTIME_IMAGE_REPO'] = (
            registry_prefix.rstrip('/') + '/runtime'
        )
        self.log(
            'debug',
            f'Runtime image repo: {os.environ["OH_RUNTIME_RUNTIME_IMAGE_REPO"]}',
        )

        if self.config.sandbox.runtime_extra_deps:
            self.log(
                'debug',
                f'Installing extra user-provided dependencies in the runtime image: {self.config.sandbox.runtime_extra_deps}',
            )

        # Build the container image
        self.container_image = build_runtime_image(
            self.config.sandbox.base_container_image,
            self.runtime_builder,
            platform=self.config.sandbox.platform,
            extra_deps=self.config.sandbox.runtime_extra_deps,
            force_rebuild=self.config.sandbox.force_rebuild_runtime,
        )

        response = self._send_request(
            'GET',
            f'{self.config.sandbox.remote_runtime_api_url}/image_exists',
            params={'image': self.container_image},
            timeout=10,
        )
        if not response.json()['exists']:
            raise RuntimeError(f'Container image {self.container_image} does not exist')

    def _start_runtime(self):
        # Prepare the request body for the /start endpoint
        plugin_args = []
        if self.plugins is not None and len(self.plugins) > 0:
            plugin_args = ['--plugins'] + [plugin.name for plugin in self.plugins]
        browsergym_args = []
        if self.config.sandbox.browsergym_eval_env is not None:
            browsergym_args = [
                '--browsergym-eval-env'
            ] + self.config.sandbox.browsergym_eval_env.split(' ')
        command = get_remote_startup_command(
            self.port,
            self.config.workspace_mount_path_in_sandbox,
            'openhands' if self.config.run_as_openhands else 'root',
            self.config.sandbox.user_id,
            plugin_args,
            browsergym_args,
        )
        start_request = {
            'image': self.container_image,
            'command': command,
            'working_dir': '/openhands/code/',
            'environment': {'DEBUG': 'true'} if self.config.debug else {},
            'runtime_id': self.sid,
        }

        # Start the sandbox using the /start endpoint
        response = self._send_request(
            'POST',
            f'{self.config.sandbox.remote_runtime_api_url}/start',
            json=start_request,
        )
        self._parse_runtime_response(response)
        self.log(
            'debug',
            f'Runtime started. URL: {self.runtime_url}',
        )

    def _resume_runtime(self):
        self._send_request(
            'POST',
            f'{self.config.sandbox.remote_runtime_api_url}/resume',
            json={'runtime_id': self.runtime_id},
            timeout=30,
        )
        self.log('debug', 'Runtime resumed.')

    def _parse_runtime_response(self, response: requests.Response):
        start_response = response.json()
        self.runtime_id = start_response['runtime_id']
        self.runtime_url = start_response['url']
        if 'session_api_key' in start_response:
            self.session.headers.update(
                {'X-Session-API-Key': start_response['session_api_key']}
            )

    @tenacity.retry(
        stop=tenacity.stop_after_delay(180) | stop_if_should_exit(),
        reraise=True,
        retry=tenacity.retry_if_exception_type(RuntimeNotReadyError),
        wait=tenacity.wait_fixed(2),
    )
    def _wait_until_alive(self):
        self.log('debug', f'Waiting for runtime to be alive at url: {self.runtime_url}')
        runtime_info_response = self._send_request(
            'GET',
            f'{self.config.sandbox.remote_runtime_api_url}/runtime/{self.runtime_id}',
        )
        runtime_data = runtime_info_response.json()
        assert 'runtime_id' in runtime_data
        assert runtime_data['runtime_id'] == self.runtime_id
        assert 'pod_status' in runtime_data
        pod_status = runtime_data['pod_status']
        if pod_status == 'Ready':
            try:
                self._send_request(
                    'GET',
                    f'{self.runtime_url}/alive',
                )  # will raise exception if we don't get 200 back.
            except requests.HTTPError as e:
                self.log(
                    'warning', f"Runtime /alive failed, but pod says it's ready: {e}"
                )
                raise RuntimeNotReadyError(
                    f'Runtime /alive failed to respond with 200: {e}'
                )
            return
        if pod_status in ('Failed', 'Unknown', 'Not Found'):
            # clean up the runtime
            self.close()
            raise RuntimeError(
                f'Runtime (ID={self.runtime_id}) failed to start. Current status: {pod_status}'
            )

        self.log(
            'debug',
            f'Waiting for runtime pod to be active. Current status: {pod_status}',
        )
        raise RuntimeNotReadyError()

    def close(self, timeout: int = 10):
        if self.config.sandbox.keep_remote_runtime_alive or self.attach_to_existing:
            self.session.close()
            return
        if self.runtime_id and self.session:
            try:
                response = self._send_request(
                    'POST',
                    f'{self.config.sandbox.remote_runtime_api_url}/stop',
                    json={'runtime_id': self.runtime_id},
                    timeout=timeout,
                )
                if response.status_code != 200:
                    self.log(
                        'error',
                        f'Failed to stop runtime: {response.text}',
                    )
                else:
                    self.log('debug', 'Runtime stopped.')
            except Exception as e:
                raise e
            finally:
                self.session.close()

    def run_action(self, action: Action) -> Observation:
        if action.timeout is None:
            action.timeout = self.config.sandbox.timeout
        if isinstance(action, FileEditAction):
            return self.edit(action)
        with self.action_semaphore:
            if not action.runnable:
                return NullObservation('')
            action_type = action.action  # type: ignore[attr-defined]
            if action_type not in ACTION_TYPE_TO_CLASS:
                raise ValueError(f'Action {action_type} does not exist.')
            if not hasattr(self, action_type):
                return ErrorObservation(
                    f'[Runtime (ID={self.runtime_id})] Action {action_type} is not supported in the current runtime.',
                    error_id='AGENT_ERROR$BAD_ACTION',
                )

            assert action.timeout is not None

            try:
                request_body = {'action': event_to_dict(action)}
                self.log('debug', f'Request body: {request_body}')
                response = self._send_request(
                    'POST',
                    f'{self.runtime_url}/execute_action',
                    json=request_body,
                    # wait a few more seconds to get the timeout error from client side
                    timeout=action.timeout + 5,
                )
                output = response.json()
                obs = observation_from_dict(output)
                obs._cause = action.id  # type: ignore[attr-defined]
            except requests.Timeout:
                raise RuntimeError(
                    f'Runtime failed to return execute_action before the requested timeout of {action.timeout}s'
                )
            return obs

    def _send_request(self, method, url, **kwargs):
        is_runtime_request = self.runtime_url and self.runtime_url in url
        try:
            return send_request(self.session, method, url, **kwargs)
        except requests.Timeout:
            self.log('error', 'No response received within the timeout period.')
            raise
        except requests.HTTPError as e:
            if is_runtime_request and e.response.status_code == 404:
                raise RuntimeDisconnectedError(
                    f'404 error while connecting to {self.runtime_url}'
                )
            else:
                raise e

    def run(self, action: CmdRunAction) -> Observation:
        return self.run_action(action)

    def run_ipython(self, action: IPythonRunCellAction) -> Observation:
        return self.run_action(action)

    def read(self, action: FileReadAction) -> Observation:
        return self.run_action(action)

    def write(self, action: FileWriteAction) -> Observation:
        return self.run_action(action)

    def browse(self, action: BrowseURLAction) -> Observation:
        return self.run_action(action)

    def browse_interactive(self, action: BrowseInteractiveAction) -> Observation:
        return self.run_action(action)

    def copy_to(
        self, host_src: str, sandbox_dest: str, recursive: bool = False
    ) -> None:
        if not os.path.exists(host_src):
            raise FileNotFoundError(f'Source file {host_src} does not exist')

        try:
            if recursive:
                with tempfile.NamedTemporaryFile(
                    suffix='.zip', delete=False
                ) as temp_zip:
                    temp_zip_path = temp_zip.name

                with ZipFile(temp_zip_path, 'w') as zipf:
                    for root, _, files in os.walk(host_src):
                        for file in files:
                            file_path = os.path.join(root, file)
                            arcname = os.path.relpath(
                                file_path, os.path.dirname(host_src)
                            )
                            zipf.write(file_path, arcname)

                upload_data = {'file': open(temp_zip_path, 'rb')}
            else:
                upload_data = {'file': open(host_src, 'rb')}

            params = {'destination': sandbox_dest, 'recursive': str(recursive).lower()}

            response = self._send_request(
                'POST',
                f'{self.runtime_url}/upload_file',
                files=upload_data,
                params=params,
                timeout=300,
            )
            self.log(
                'debug',
                f'Copy completed: host:{host_src} -> runtime:{sandbox_dest}. Response: {response.text}',
            )
        finally:
            if recursive:
                os.unlink(temp_zip_path)
            self.log(
                'debug', f'Copy completed: host:{host_src} -> runtime:{sandbox_dest}'
            )

    def list_files(self, path: str | None = None) -> list[str]:
        data = {}
        if path is not None:
            data['path'] = path

        response = self._send_request(
            'POST',
            f'{self.runtime_url}/list_files',
            json=data,
            timeout=30,
        )
        response_json = response.json()
        assert isinstance(response_json, list)
        return response_json

    def copy_from(self, path: str) -> bytes:
        """Zip all files in the sandbox and return as a stream of bytes."""
        params = {'path': path}
        response = self._send_request(
            'GET',
            f'{self.runtime_url}/download_files',
            params=params,
            timeout=30,
        )
        return response.content
