# SPDX-FileCopyrightText: 2024, G.A. vd. Hoorn
# SPDX-License-Identifier: Apache-2.0
#
# Parts of this file based on (generated) content from
# 'github.com/osrf/osrf/docker_images', which is also licensed under the
# Apache 2.0 license.

FROM {{ base_docker_image }} AS build_env

# the many RUNs are inefficient but get the job done and this image is not
# intended for distribution, so minimising size is less important

ARG DEBIAN_FRONTEND="noninteractive"

# mimic the cloud-init script
RUN apt-get update \
 && apt-get upgrade -y

# from upstream osrf/docker_images
RUN echo 'Etc/UTC' > /etc/timezone \
 && ln -s /usr/share/zoneinfo/Etc/UTC /etc/localtime \
 && apt-get install -q -y --no-install-recommends tzdata

RUN apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      dirmngr \
      gnupg2 \
      lsb-release

# from upstream osrf/docker_images: setup keys
RUN set -eux; \
      key='C1CF6E31E6BADE8868B172B4F42ED6FBAB17C654'; \
      export GNUPGHOME="$(mktemp -d)"; \
      gpg --batch --keyserver keyserver.ubuntu.com --recv-keys "$key"; \
      mkdir -p /usr/share/keyrings; \
      gpg --batch --export "$key" > /usr/share/keyrings/ros-latest-archive-keyring.gpg; \
      gpgconf --kill all; \
      rm -rf "$GNUPGHOME"
RUN echo "deb [arch={{ machine }} signed-by=/usr/share/keyrings/ros-latest-archive-keyring.gpg ] http://packages.ros.org/ros/ubuntu {{ distro_release }} main" > /etc/apt/sources.list.d/ros-latest.list

# setup environment
ENV LANG C.UTF-8
ENV LC_ALL C.UTF-8

# install build / dev tools
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential \
      cmake \
      git \
      python3 \
      python3-colcon-common-extensions \
      python3-pip \
      python3-rosdep

# work around https://github.com/docker/docker-py/issues/3246
RUN echo 'if [ -f /opt/ros/*/setup.bash ]; then' >> /root/.bashrc \
 && echo '  . /opt/ros/*/setup.bash'             >> /root/.bashrc \
 && echo 'fi'                                    >> /root/.bashrc

# used as CWD by Docker provider 'execute_command(..)'
WORKDIR /ws

CMD ["bash"]
