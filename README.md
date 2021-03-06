# APM Integration Testing 

This repo contains tools for end to end (eg agent -> apm server -> elasticsearch <- kibana) development and testing of Elastic APM.

[![Build Status](https://apm-ci.elastic.co/view/All/job/elastic+apm-integration-testing+master+push/badge/icon?style=plastic)](https://apm-ci.elastic.co/job/elastic+apm-integration-testing+master+push/)

## Prerequisites

The basic requirements for starting a local environment are:

- Docker
- Python (version 3 preferred)

This repo is tested with Python 3 but best effort is made to make starting/stopping environments work with Python 2.7.

### Docker

[Installation instructions](https://www.docker.com/community-edition)

### Python 3

- Windows: [Installation instructions](https://www.python.org/downloads/windows/)
- Mac (using [Homebrew](https://brew.sh/)):
  ```sh
  brew install python
  ```
- Debian/Ubuntu
  ```sh
  sudo apt-get install python3
  ```

## Running Local Enviroments

### Starting an Environment

`./scripts/compose.py` provides a handy cli for starting a testing environment using docker-compose.
`make venv` creates a virtual environment with all of the python-based dependencies needed to run `./scripts/compose.py` - it requires `virtualenv` in your `PATH`.
Activate the virtualenv with `source venv/bin/activate` and use `./scripts/compose.py --help` for information on subcommands and arguments.

### Stopping an Environment

All services:
```
./scripts/compose.py stop

# OR

docker-compose down
```

All services and clean up volumes and networks:
```
make stop-env
```

Individual services:
```
docker-compose stop <service name>
```

## Example environments

### Change default ports

Expose Kibana on http://localhost:1234:

    ./scripts/compose.py start master --kibana-port 1234

### Opbeans

Opbeans are demo web applications that are instrumented with Elastic APM.
Start `opbeans-*` services and their dependencies along with apm-server, elasticsearch, and kibana:

    ./scripts/compose.py start --all master


This will also start the `opbeans-load-generator` service which, by default,
will generate random requests to all started backend Opbeans services.
To disable load generation for a specific service, use the `--no-opbeans-XYZ-loadgen` flag.

Opbeans RUM does not need a load generation service,
as it is itself generating load using a headless chrome instance.

### Uploading Sourcemaps

The frontend app packaged with opbeans-node runs in a production build, which means the source code is minified. The APM server needs the corresponding sourcemap to unminify the code.

You can upload the sourcemap with this command:

    ./scripts/compose.py upload-sourcemap

In the standard setup, it will find the config options itself, but they can be overwritten. See

    ./scripts/compose.py upload-sourcemap --help

### Kafka output

    ./scripts/compose.py start --with-kafka --with-zookeeper --apm-server-output=kafka --with-logstash master

Logstash will be configured to ingest events from kafka.

Topics are named according to service name. To view events for 1234_service-12a3:

    docker exec -it localtesting_6.3.0-SNAPSHOT_kafka kafka-console-consumer --bootstrap-server kafka:9092 --topic apm-1234_service-12a3 --from-beginning --max-messages 100

Onboarding events will go to the apm topic.

Note that index templates are not loaded automatically when using outputs other than Elasticsearch.  Create them manually with:

    ./scripts/compose.py load-dashboards

If data was inserted before this point (eg an opbeans service was started) you'll probably have to delete the auto-created `apm-*` indexes and let them be recreated.

## Advanced topics

### Dumping docker-compose.yml

`./scripts/compose.py start master --docker-compose-path - --skip-download` will dump the generated `docker-compose.yml` to standard out (`-`) without starting any containers or downloading images.

Omit `--skip-download` to just download images.

### Testing compose

`compose.py` includes unittests, `make test-compose` to run.

## Running Tests

Additional dependencies are required for running the integration tests:
- python3
- virtualenv

On a Mac with Homebrew:

```sh
brew install pyenv-virtualenv
```

All integration tests are written in python and live under `tests/`.

Several `make` targets exist to make their execution simpler:

- test-server
- test-kibana
- test-agent-{go,node,python,ruby,...}

These targets will create a python virtual environment in `venv` with all of the dependencies need to run the suite.

Each target requires a running test environment, providing an apm-server, elasticsearch, and others depending on the particular suite.

Tests should always eventually be run within a docker container to ensure a consistent, repeatable environment for reporting.

Prefix any of the `test-` targets with `docker-` to run them in a container eg: `make docker-test-server`.

### Continuous Integration

Jenkins runs the scripts from `scripts/ci/` and is viewable at https://apm-ci.elastic.co/.

Those scripts shut down any existing testing containers and start a fresh new environment before running tests unless the `REUSE_CONTAINERS` environment variable is set.

#### Version tests

Various combinations of versions of agents and the Elastic Stack are tested together to ensure compatibility.
The matrix is defined using [apm_server.yml](https://github.com/elastic/apm-integration-testing/blob/master/tests/versions/apm_server.yml) for one axis and then a per-agent specification for the other axis.
Certain exclusions are defined on a per agent basis.
For example, [the nodejs matrix](https://apm-ci.elastic.co/view/All/job/elastic+apm-integration-testing+master+multijob-nodejs-agent-versions/) is defined in [nodejs.yml](https://github.com/elastic/apm-integration-testing/blob/master/tests/versions/nodejs.yml).
When those tests run, `scripts/ci/versions_nodejs.sh` is invoked with the product of those files, eg `scripts/ci/versions_nodejs.sh 'github;master' '6.3'`.
The Elastic Stack version argument accepts an optional list of semi-colon separated arguments that will be passed to `scripts/compose.py` when building the test stack.

### Agent Development

To run integration tests against unreleased agent code, start an environment where that agent code is used by the test application.

For example, to test an update to the python agent from user `elasticcontributor` on their `newfeature1` branch:

```bash
# start test deps: apm-server, elasticsearch, and the two python test applications
# the test applications will use elasticcontributor's newfeature1 apm agent
./scripts/compose.py start master --no-kibana --with-agent-python-django --with-agent-python-flask --python-agent-package=git+https://github.com/elasticcontributor/apm-agent-python.git@newfeature1 --force-build

# wait for healthiness
docker-compose ps

# run tests
make test-agent-python
```

Testing unrelease code for other agents follows a simliar pattern.

See `version*` in https://github.com/elastic/apm-integration-testing/tree/master/scripts/ci for details on how CI tests specific agent/elastic stack version combinations.
