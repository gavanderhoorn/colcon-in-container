# SPDX-FileCopyrightText: 2024, G.A. vd. Hoorn
# SPDX-License-Identifier: GPL-3.0-only

import docker
import io
import os
import pathlib
import sys
import tarfile
import time
import typing as t

from platform import system
import subprocess

from colcon_in_container.logging import logger
from colcon_in_container.providers import exceptions
from colcon_in_container.providers.provider import Provider


WS_DIR='/ws'
TMP_SCRIPT='/tmp/script'
CONTAINER_START_TIMEOUT=5.0
DOCKER_CONTAINER_STATUS_RUNNING='running'


cloud_init_script_as_bash_script="""#!/bin/bash
export DEBIAN_FRONTEND="noninteractive"
apt-get -qq update
apt-get -q upgrade -y

apt-get -q install -y --no-install-recommends \
  ca-certificates \
  curl \
  lsb-release
curl -sSL \
  https://raw.githubusercontent.com/ros/rosdistro/master/ros.key \
  -o /usr/share/keyrings/ros-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/ros-archive-keyring.gpg] http://packages.ros.org/ros2/ubuntu $(. /etc/os-release && echo $UBUNTU_CODENAME) main" > /etc/apt/sources.list.d/ros2.list

apt-get -qq update
apt-get -q install -y --no-install-recommends \
  build-essential \
  cmake \
  git \
  python3 \
  python3-colcon-common-extensions \
  python3-pip \
  python3-rosdep

cat << EOF >> /root/.bashrc
if [ -f /opt/ros/*/setup.bash ]; then
. /opt/ros/*/setup.bash
fi
cd /ws
EOF

mkdir -p /ws
"""


class DockerClient(Provider):
    """Docker client interacting with the Docker socket."""

    def __init__(self, ros_distro):  # noqa: D107
        super().__init__(ros_distro)
        logger.info("attempting to initialise Docker provider")
        if system() != 'Linux':
            raise exceptions.ProviderDoesNotSupportHostOSError(
                'DockerClient is only supported on Linux')

        self._container = None

        try:
            self._client = docker.from_env()
        except Exception as e:
            raise exceptions.ProviderClientError(
                'Failed to initialized Docker client. '
                f'Make sure Docker is properly installed and running: {e}'
            )

        # see if container already exists, if so: delete it.
        # Stop it first if it's running
        try:
            container = self._client.containers.get(self.instance_name)
            logger.debug(f"attempting to delete existing container (status: '{container.status}') ..")
            container.remove(v=True, force=True)
        except docker.errors.NotFound as nf:
            logger.debug(f"no existing container with name '{self.instance_name}' found")
        except docker.errors.APIError as ae:
            raise exceptions.ProviderClientError(f'Docker API error: {ae}')

        # make sure it was successfully removed
        try:
            # https://github.com/docker/docker-py/issues/2860
            container = self._client.containers.get(self.instance_name)
            if container.status == DOCKER_CONTAINER_STATUS_RUNNING:
                raise exceptions.ProviderClientError(
                    f"Error stopping container '{self.instance_name}'")
        except docker.errors.NotFound as nf:
            pass
        except docker.errors.APIError as ae:
            raise exceptions.ProviderClientError(f'Docker API error: {ae}')

        # start a new instance
        docker_image = f'ubuntu:{self.ubuntu_distro}'
        logger.debug(f"starting container '{self.instance_name}' from image: '{docker_image}' ..")

        # mimic '-ti' using 'stdin_open' and 'tty'. See https://stackoverflow.com/a/75128852
        # and https://github.com/docker/docker-py/issues/390
        self._container = self._client.containers.run(
            image=docker_image,
            # TODO(gavanderhoorn): this assumes bash is available (but perhaps that is OK)
            command='/bin/bash',
            name=self.instance_name,
            detach=True,
            stdin_open=True,
            tty=True)

        # make sure it's started, it takes a while in some cases
        _wait_for_container_status(
            container=self._container,
            status=DOCKER_CONTAINER_STATUS_RUNNING,
            timeout=CONTAINER_START_TIMEOUT)
        logger.debug(f"container running (id: {self._container.short_id})")

        # make sure to create '/ws' before running anything else (as execute_command(..)
        # uses it as the CWD)
        (exit_code, _) = self._container.exec_run(cmd=['mkdir', '-p', WS_DIR])
        if exit_code != 0:
            raise exceptions.ProviderClientError(f"Couldn't create '{WS_DIR}': {exit_code}")

        # TODO: use actual cloud-init file instead
        self._fake_cloud_init_install()

        logger.info('container running and initialised: '
            f"('{self.instance_name}' ({self._container.short_id}) from '{docker_image}')")

    def _check_instance(self):
        if not hasattr(self, '_container') or not self._container:
            raise exceptions.ProviderNotConfiguredError("No running container")

    def _clean_instance(self):
        if container := self._client.containers.get(self.instance_name):
            # don't care whether this succeeds
            container.remove(v=True, force=True)
            if hasattr(self, '_container'):
                self._container = None

    def _fake_cloud_init_install(self):
        self._write_in_instance(instance_file_path=TMP_SCRIPT, lines=cloud_init_script_as_bash_script)
        if (exit_code := self.execute_command(['bash', '-xei', TMP_SCRIPT])) != 0:
            raise exceptions.ProviderClientError(f"Error installing initial packages: {exit_code}")

    def wait_for_install(self):
        # no-op, as ctor should have already installed everything
        pass

    def execute_command(self, command):
        """Execute the given command inside the instance."""
        self._check_instance()

        # can't use Container.exec_run(..) because of
        # https://github.com/docker/docker-py/issues/2450 and
        # https://github.com/docker/docker-py/issues/2557.
        # Instead , setup a command to execute, then start it, relay output and
        # finally check the exit code manually after the command finished.
        exec_id = self._client.api.exec_create(self._container.name, command, tty=True, workdir=WS_DIR)
        stream = self._client.api.exec_start(exec_id, detach=False, stream=True)
        for l in stream:
            self.logger_instance.debug(l.decode('utf-8').rstrip('\n'))
        exec_status = self._client.api.exec_inspect(exec_id)
        return exec_status['ExitCode']

    def _copy_from_instance_to_host(self, *, instance_path, host_path):
        """Copy data from the instance to the host."""
        self._check_instance()
        _recursive_get(container=self._container, remote_path=instance_path, local_path=host_path)

    def _copy_from_host_to_instance(self, *, host_path, instance_path):
        """Copy data from the host to the instance."""
        self._check_instance()
        _recursive_put(container=self._container, local_path=host_path, remote_path=instance_path)

    def _write_in_instance(self, *, instance_file_path, lines):
        """Copy data from the instance to the host."""
        self._check_instance()

        dst_filename = pathlib.Path(instance_file_path).name
        dst_path = pathlib.Path(instance_file_path).parent
        buf = io.BytesIO()
        data = lines.encode('utf-8')
        with tarfile.open(fileobj=buf, mode="w|", format=tarfile.GNU_FORMAT) as tar_f:
            t_info = tarfile.TarInfo(dst_filename)
            t_info.size = len(data)
            tar_f.addfile(t_info, io.BytesIO(data))
        buf.seek(0)

        if not self._container.put_archive(path=dst_path, data=buf.getvalue()):
            raise exceptions.ProviderClientError("put_archive failed")

    def shell(self):
        """Shell into the instance."""
        self._check_instance()
        subprocess.run(['docker', 'exec', '-ti', self.instance_name, '/bin/bash'])


def _wait_for_container_status(container, status: t.Union[str, t.List[str]], timeout: t.Optional[float] = 5.0):
    if not container:
        raise ValueError("No container")
    if timeout < 0.:
        raise ValueError(f"timeout can't be negative ({timeout})")
    if type(status) == str:
        status = [status]
    t_start = time.time()
    t_timeout = t_start + timeout
    container.reload()
    while not (container.status in status) and (time.time() - t_start) < t_timeout:
        logger.debug(f"(still) waiting for '{container.name}' state to change to '{status}' ..")
        time.sleep(1.0)
        container.reload()
    if not (container.status in status):
        raise exceptions.ProviderClientError(
            f"time-out waiting for state '{status}' (current state: '{container.status}')")


def _recursive_put(container, local_path: str, remote_path: str, exclude_parent: t.Optional[bool] = True):
    """Copy files from 'local_path' on the host to 'remote_path' in the container

    NOTE: this supports directories ONLY. No individual files."""
    norm_local_path = os.path.normpath(local_path)
    if not os.path.isdir(norm_local_path):
        raise NotADirectoryError('local_path parameter must be a directory')

    # set arcname to empty string to prevent 'local_path' itself from being
    # included in the archive. It's 'None' by default, so set it to that if
    # caller does want it included
    arcname = "" if exclude_parent else None

    # create an in-memory tar archive containing the children of 'local_path'
    # and push it to the container. put_archive(..) will extract it for us
    # inside the container at 'remote_path'
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w|', format=tarfile.GNU_FORMAT) as tar_f:
        # recursive=True is the default, but let's be crystal clear about
        # what we're doing here.
        tar_f.add(norm_local_path, arcname=arcname, recursive=True)
    buf.seek(0)
    if not container.put_archive(path=remote_path, data=buf.getvalue()):
        raise exceptions.ProviderClientError("put_archive failed")


def _recursive_get(container, remote_path: str, local_path: str):
    """Copy files from 'remote_path' in container to 'local_path' on host"""
    tar_data, path_stat = container.get_archive(path=remote_path)
    buf = io.BytesIO()
    # TODO(gavanderhoorn) this might use quite some memory for large (sets of)
    # files, see if an actual file on-disk (host side) would be better.
    # Most use in this class should be OK though.
    for d in tar_data:
        buf.write(d)
    buf.seek(0)
    with tarfile.open(fileobj=buf, mode='r|', format=tarfile.GNU_FORMAT) as tar_f:
        if sys.version_info >= (3, 12):
            tar_f.extractall(path=local_path, filter="data")
        else:
            tar_f.extractall(path=local_path)
