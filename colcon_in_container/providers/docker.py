# SPDX-FileCopyrightText: 2024, G.A. vd. Hoorn
# SPDX-License-Identifier: GPL-3.0-only

import docker
import io
import jinja2
import os
import pathlib
import sys
import tarfile
import time
import typing as t

from platform import system, machine
import subprocess

from colcon_in_container.logging import logger
from colcon_in_container.providers import exceptions
from colcon_in_container.providers._helper \
    import host_architecture
from colcon_in_container.providers.provider import Provider


WS_DIR='/ws'
TMP_SCRIPT='/tmp/script'
CONTAINER_START_TIMEOUT=5.0
DOCKER_IMAGE_PREFIX='colcon-in-container'
DOCKER_CONTAINER_STATUS_RUNNING='running'


class DockerClient(Provider):
    """Docker client interacting with the Docker socket."""

    @staticmethod
    def register_args(parser):
        parser.add_argument(
            '--docker-image',
            metavar='IMAGE',
            type=str,
            default='',
            help='Docker image to use for builds instead of default ubuntu image.'
            ' Only used by Docker provider.'
        )
        parser.add_argument(
            '--docker-force-build',
            action='store_true',
            help='Always build Docker image, even if it exists (locally).',
        )

    def __init__(self, cli_args):  # noqa: D107
        super().__init__(cli_args)
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

        # determine which Docker image to build packages in: either one we build
        # here, or one user has provided
        if cli_args.docker_image:
            logger.debug(
                f"checking specified image ('{cli_args.docker_image}') exists (locally)")

            # warn user they requested a rebuild of an image we don't 'own'
            if cli_args.docker_force_build:
                logger.warn(
                    "requested forced (re)build of 3rd party image, ignoring request")

            # user specified image. Check it exists (locally). If it doesn't,
            # we can't continue (as we're not building anything ourselves),
            # so abort
            if not _image_exists(client=self._client, name=cli_args.docker_image):
                raise exceptions.ProviderClientError(
                    f"No such Docker image '{cli_args.docker_image}', aborting")
            self.docker_image = cli_args.docker_image

            # TODO(gavanderhoorn): check 3rd party image for required (build) tools

        # nothing specified: build our own (if needed or forced to)
        else:
            logger.debug(
                "no image specified, using default (for specified ROS distro)")

            # ROS distro has already been mapped to Ubuntu distro earlier
            base_docker_image = f'ubuntu:{self.ubuntu_distro}'
            logger.debug(f"using base Docker image: '{base_docker_image}'")

            # construct image name, based on target ROS distribution (and
            # required Ubuntu version)
            docker_image = f'{DOCKER_IMAGE_PREFIX}:{self.ubuntu_distro}-{self.ros_distro}'

            # see whether user specified to always (re)build the image. If so,
            # delete it first if it exists
            if cli_args.docker_force_build and _image_exists(client=self._client, name=docker_image):
                logger.info(
                    f"forced build requested, deleting existing image before rebuild")
                self._client.images.remove(image=docker_image, force=True)

            # only build if it doesn't exist already
            if not _image_exists(client=self._client, name=docker_image):
                # render template, basing it on image identified earlier
                dockerfile_content = self._render_jinja_template(
                    base_docker_image=base_docker_image)

                # build the actual image
                logger.info(
                    f"building Docker image '{docker_image}' .. (this may take some time)")
                buf = io.BytesIO(dockerfile_content.encode('utf-8'))
                try:
                    # TODO(gavanderhoorn): maybe hook-up docker build logs to logger.debug
                    # https://docker-py.readthedocs.io/en/stable/api.html#module-docker.api.build
                    image, logs = self._client.images.build(fileobj=buf, tag=docker_image, rm=True)
                except docker.errors.BuildError as be:
                    raise exceptions.ProviderNotConfiguredError(f"Image build error: {be}")
                except docker.errors.APIError as ae:
                    raise exceptions.ProviderClientError(f'Docker API error: {ae}')

                # make sure it succeeded. If it didn't, abort
                if not _image_exists(client=self._client, name=docker_image):
                    raise exceptions.ProviderClientError(
                        f"Docker image build failed, aborting")
                logger.debug(f"built image")
                self.docker_image = docker_image

            else:
                logger.info(f"reusing existing image ('{docker_image}')")
                self.docker_image = docker_image

        # at this point either our own image, or user-specified image should be
        # available. Start a new instance
        logger.debug(
            f"starting container '{self.instance_name}' from image: '{self.docker_image}' ..")

        # mimic '-ti' using 'stdin_open' and 'tty'. See https://stackoverflow.com/a/75128852
        # and https://github.com/docker/docker-py/issues/390
        self._container = self._client.containers.run(
            image=self.docker_image,
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

        logger.info('container running and initialised: '
            f"('{self.instance_name}' ({self._container.short_id}) from '{self.docker_image}')")

    def _render_jinja_template(self, base_docker_image: str) -> str:
        config_directory = os.path.join(
            os.path.dirname(os.path.realpath(__file__)), 'config')
        cloud_init_file = os.path.join(config_directory, 'Dockerfile.em')
        with open(cloud_init_file, 'r') as f:
            docker_file_em = f.read()

        template = jinja2.Environment().from_string(source=docker_file_em)
        return template.render(
            {'machine': host_architecture(),
             'base_docker_image': base_docker_image,
             'distro_release': self.ubuntu_distro,
             'ros_distro': self.ros_distro}
        )

    def _check_instance(self):
        if not hasattr(self, '_container') or not self._container:
            raise exceptions.ProviderNotConfiguredError("No running container")

    def _clean_instance(self):
        if container := self._client.containers.get(self.instance_name):
            # don't care whether this succeeds
            container.remove(v=True, force=True)
            if hasattr(self, '_container'):
                self._container = None

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


def _image_exists(client, name: str) -> bool:
    try:
        client.images.get(name=name)
        return True
    except docker.errors.NotFound as nf:
        return False
    except docker.errors.APIError as ae:
        raise exceptions.ProviderClientError(f'Docker API error: {ae}')


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
