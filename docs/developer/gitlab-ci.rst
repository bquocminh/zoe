.. _ci-gitlab:

Zoe Continuous Integration with Gitlab pipelines
================================================

Overview
--------

GitLab offers an `integrated way of running a CI pipeline <https://docs.gitlab.com/ce/ci/README.html>`_ via the GitLab runners system. The Zoe repository contains the pipeline description in the ``.gitlab-ci.yml`` file.

To use it, we suggest to configure the GitLab runner (`documentation <https://docs.gitlab.com/runner/>`_) with the Docker executor. This ensures tests are run in a clean and isolated environment.

Runner configuration
--------------------

Using the Docker executor, we configured our runner with these options::

    concurrent = 5
    check_interval = 0

    [[runners]]
      name = "ci-server"
      url = "https://gitlab.eurecom.fr/ci"
      token = "<your-token-here>"
      executor = "docker"
      environment = ["DOCKER_AUTH_CONFIG={ \"auths\": { \"docker-registry:5000\": { \"auth\": \"some-base64-string\" } } }", "DOCKER_REGISTRY=docker-registry:5000"]
      [runners.docker]
        image = "docker:latest"
        disable_cache = false
        volumes = ["/cache", "/tls/certificates/ca.crt:/registry-ca.crt:ro", "/var/run/docker.sock:/var/run/docker.sock"]
      [runners.cache]
        Insecure = false

Please note that since our private registry is protected with TLS, we need to pass also the CA certificate to be able to push Docker images build inside the CI pipeline.

Variables
---------

To run the tests a number of variables need to be set from the GitLab interface:

* REGISTRY_PASSWORD: the password used for authenticating with the registry via docker login
* SSH_PRIVATE_KEY: private key to be used to deploy via rsync the staging build
* STAGING_IP: IP/hostname of the staging server
* WEB_STAGING_PATH: path for the web interface on the staging server
* ZOE_STAGING_PATH: path for Zoe on the staging server
