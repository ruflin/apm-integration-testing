#!/usr/bin/env python
"""
CLI for starting a testing environment using docker-compose.
"""
from __future__ import print_function

from abc import abstractmethod

import argparse
import collections
import datetime
import functools
import glob
import inspect
import json
import logging
import multiprocessing
import os
import re
import sys
import subprocess

try:
    from urllib.request import urlopen, urlretrieve, Request
except ImportError:
    from urllib import urlretrieve
    from urllib2 import urlopen, Request

#
# package info
#
PACKAGE_NAME = 'localmanager'
__version__ = "4.0.0"

DEFAULT_STACK_VERSION = "6.3.3"


#
# helpers
#
def _camel_hyphen(string):
    return re.sub(r'([a-z])([A-Z])', r'\1-\2', string)


def discover_services(mod=None):
    """discover list of services"""
    ret = []
    if not mod:
        mod = sys.modules[__name__]
    for obj in dir(mod):
        cls = getattr(mod, obj)
        if inspect.isclass(cls) and issubclass(cls, Service) \
                and cls not in (Service, OpbeansService):
            ret.append(cls)
    return ret


def _load_image(cache_dir, url):
    filename = os.path.basename(url)
    filepath = os.path.join(cache_dir, filename)
    etag_cache_file = filepath + '.etag'
    if os.path.exists(etag_cache_file):
        with open(etag_cache_file, mode='r') as f:
            etag = f.read().strip()
    else:
        etag = None
    request = Request(url)
    request.get_method = lambda: 'HEAD'
    try:
        response = urlopen(request)
    except Exception as e:
        print('Error while fetching %s: %s' % (url, str(e)))
        return False
    new_etag = response.info().get('ETag')
    if etag == new_etag:
        print("Skipping download of %s, local file is current" % filename)
        return True
    print("downloading", url)
    try:
        os.makedirs(cache_dir)
    except Exception:  # noqa: E722
        pass  # ignore
    try:
        urlretrieve(url, filepath)
    except Exception as e:
        print('Error while fetching %s: %s' % (url, str(e)))
        return False
    subprocess.check_call(["docker", "load", "-i", filepath])
    with open(etag_cache_file, mode='w') as f:
        f.write(new_etag)
    return True


def load_images(urls, cache_dir):
    load_image_fn = functools.partial(_load_image, cache_dir)
    pool = multiprocessing.Pool(4)
    # b/c python2
    try:
        results = pool.map_async(load_image_fn, urls).get(timeout=10000000)
    except KeyboardInterrupt:
        pool.terminate()
        raise
    if not all(results):
        print("Errors while downloading. Exiting.")
        sys.exit(1)


DEFAULT_HEALTHCHECK_INTERVAL = "5s"
DEFAULT_HEALTHCHECK_RETRIES = 12


def curl_healthcheck(port, host="localhost", path="/healthcheck",
                     interval=DEFAULT_HEALTHCHECK_INTERVAL, retries=DEFAULT_HEALTHCHECK_RETRIES):
    return {
                "interval": interval,
                "retries": retries,
                "test": ["CMD", "curl", "--write-out", "'HTTP %{http_code}'", "--fail", "--silent",
                         "--output", "/dev/null",
                         "http://{}:{}{}".format(host, port, path)]
            }


def parse_version(version):
    res = []
    for x in version.split('.'):
        try:
            y = int(x)
        except ValueError:
            y = int(x.split("-", 1)[0])
        res.append(y)
    return res


class Service(object):
    """encapsulate docker-compose service definition"""

    # is this a side car service for opbeans. If yes, it will automatically
    # start if any opbeans service starts
    opbeans_side_car = False

    def __init__(self, **options):
        self.options = options

        if not hasattr(self, "docker_registry"):
            self.docker_registry = "docker.elastic.co"
        if not hasattr(self, "docker_name"):
            self.docker_name = self.name()
        if not hasattr(self, "docker_path"):
            self.docker_path = self.name()

        if hasattr(self, "SERVICE_PORT"):
            self.port = options.get(self.option_name() + "_port", self.SERVICE_PORT)

        self._bc = options.get(self.option_name() + "_bc") or options.get("bc")
        self._oss = options.get(self.option_name() + "_oss") or options.get("oss")
        self._release = options.get(self.option_name() + "_release") or options.get("release")
        self._snapshot = options.get(self.option_name() + "_snapshot") or options.get("snapshot")

        # version is service specific or stack or default
        self._version = options.get(self.option_name() + "_version") or options.get("version", DEFAULT_STACK_VERSION)

    @property
    def bc(self):
        return self._bc

    def default_container_name(self):
        return "_".join(("localtesting", self.version, self.name()))

    def default_image(self, version_override=None):
        """default container image path constructor"""
        image = "/".join((self.docker_registry, self.docker_path, self.docker_name))
        if self.oss:
            image += "-oss"
        image += ":" + (version_override or self.version)
        # no command line option for setting snapshot, snapshot == no bc and not release
        if self.snapshot or not (any((self.bc, self.release))):
            image += "-SNAPSHOT"
        return image

    def default_labels(self):
        return ["co.elatic.apm.stack-version=" + self.version]

    @staticmethod
    def default_logging():
        return {
            "driver": "json-file",
            "options": {
                "max-file": "5",
                "max-size": "2m"
            }
        }

    @staticmethod
    def enabled():
        return False

    def at_least_version(self, target):
        return parse_version(self.version) >= parse_version(target)

    @classmethod
    def name(cls):
        return _camel_hyphen(cls.__name__).lower()

    @classmethod
    def option_name(cls):
        return cls.name().replace("-", "_")

    @property
    def oss(self):
        return self._oss

    @staticmethod
    def publish_port(external, internal, expose=False):
        addr = "" if expose else "127.0.0.1:"
        return addr + ":".join((str(external), str(internal)))

    @property
    def release(self):
        return self._release

    @property
    def snapshot(self):
        return self._snapshot

    def render(self):
        content = self._content()
        content.update(dict(
            container_name=content.get("container_name", self.default_container_name()),
            image=content.get("image", self.default_image()),
            labels=content.get("labels", self.default_labels()),
            logging=content.get("logging", self.default_logging())
        ))
        for prune in "image", "labels", "logging":
            if content[prune] is None:
                del (content[prune])

        return {self.name(): content}

    @property
    def version(self):
        return self._version

    @classmethod
    def add_arguments(cls, parser):
        """add service-specific command line arguments"""
        # allow port overrides
        if hasattr(cls, 'SERVICE_PORT'):
            parser.add_argument(
                '--' + cls.name() + '-port',
                type=int,
                default=cls.SERVICE_PORT,
                dest=cls.option_name() + '_port',
                help="service port"
            )

    def image_download_url(self):
        pass

    @abstractmethod
    def _content(self):
        pass


class StackService(object):
    """Mix in for Elastic services that have public docker images built but not available in a registry [yet]"""

    def image_download_url(self):
        # Elastic releases are public
        if self.release or not self.bc:
            return

        version = self.version
        image = self.docker_name
        if self.oss:
            image += "-oss"
        return "https://staging.elastic.co/{version}-{sha}/docker/{image}-{version}.tar.gz".format(
            sha=self.bc,
            image=image,
            version=version,
        )

    @classmethod
    def add_arguments(cls, parser):
        super(StackService, cls).add_arguments(parser)
        for image_detail_key in ("bc", "version"):
            parser.add_argument(
                "--" + cls.name() + "-" + image_detail_key,
                type=str,
                dest=cls.option_name() + "_" + image_detail_key,
                help="stack {} override".format(image_detail_key),
            )
        for image_detail_key in ("oss", "release", "snapshot"):
            parser.add_argument(
                "--" + cls.name() + "-" + image_detail_key,
                action="store_true",
                dest=cls.option_name() + "_" + image_detail_key,
                help="stack {} override".format(image_detail_key),
            )


#
# Elastic Services
#
class ApmServer(StackService, Service):
    docker_path = "apm"

    SERVICE_PORT = "8200"
    DEFAULT_MONITOR_PORT = "6060"
    DEFAULT_OUTPUT = "elasticsearch"
    OUTPUTS = {"elasticsearch", "kafka", "logstash"}

    def __init__(self, **options):
        super(ApmServer, self).__init__(**options)

        self.apm_server_command_args = [
            ("apm-server.frontend.enabled", "true"),
            ("apm-server.frontend.rate_limit", "100000"),
            ("apm-server.host", "0.0.0.0:8200"),
            ("apm-server.read_timeout", "1m"),
            ("apm-server.shutdown_timeout", "2m"),
            ("apm-server.write_timeout", "1m"),
            ("logging.json", "true"),
            ("logging.metrics.enabled", "false"),
            ("setup.kibana.host", "kibana:5601"),
            ("setup.template.settings.index.number_of_replicas", "0"),
            ("setup.template.settings.index.number_of_shards", "1"),
            ("setup.template.settings.index.refresh_interval", "1ms"),
            ("xpack.monitoring.elasticsearch", "true"),
            ("xpack.monitoring.enabled", "true")
        ]
        self.depends_on = {"elasticsearch": {"condition": "service_healthy"}}
        self.build = self.options.get("apm_server_build")

        if self.options.get("enable_kibana", True):
            self.depends_on["kibana"] = {"condition": "service_healthy"}
            if options.get("apm_server_dashboards", True):
                self.apm_server_command_args.append(
                    ("setup.dashboards.enabled", "true")
                )

        self.apm_server_monitor_port = options.get("apm_server_monitor_port", self.DEFAULT_MONITOR_PORT)
        self.apm_server_output = options.get("apm_server_output", self.DEFAULT_OUTPUT)
        if self.apm_server_output == "elasticsearch":
            self.apm_server_command_args.extend([
                ("output.elasticsearch.enabled", "true"),
                ("output.elasticsearch.hosts", "[elasticsearch:9200]"),
            ])
        else:
            self.apm_server_command_args.extend([
                ("output.elasticsearch.enabled", "false"),
                ("output.elasticsearch.hosts", "[elasticsearch:9200]"),
                ("xpack.monitoring.elasticsearch.hosts", "[\"elasticsearch:9200\"]"),
            ])
            if self.apm_server_output == "kafka":
                self.apm_server_command_args.extend([
                    ("output.kafka.enabled", "true"),
                    ("output.kafka.hosts", "[\"kafka:9092\"]"),
                    ("output.kafka.topics", "[{default: 'apm', topic: 'apm-%{[context.service.name]}'}]"),
                ])
            elif self.apm_server_output == "logstash":
                self.apm_server_command_args.extend([
                    ("output.logstash.enabled", "true"),
                    ("output.logstash.hosts", "[\"logstash:5044\"]"),
                ])

        self.apm_server_count = options.get("apm_server_count", 1)

    @classmethod
    def add_arguments(cls, parser):
        super(ApmServer, cls).add_arguments(parser)
        parser.add_argument(
            '--apm-server-build',
            help='build apm-server from a git repo[@branch], eg https://github.com/elastic/apm-server.git@v2'
        )
        parser.add_argument(
            '--apm-server-output',
            choices=cls.OUTPUTS,
            default='elasticsearch',
            help='apm-server output'
        )
        parser.add_argument(
            '--apm-server-count',
            type=int,
            default=1,
            help="apm-server count. >1 adds a load balancer service to round robin traffic between servers.",
        )
        parser.add_argument(
            "--no-apm-server-dashboards",
            action="store_false",
            dest="apm_server_dashboards",
            help="skip loading apm-server dashboards (setup.dashboards.enabled=false)",
        )

    def _content(self):
        command_args = []
        for param, value in self.apm_server_command_args:
            command_args.extend(["-E", param + "=" + value])

        content = dict(
            cap_add=["CHOWN", "DAC_OVERRIDE", "SETGID", "SETUID"],
            cap_drop=["ALL"],
            command=["apm-server", "-e", "--httpprof", ":{}".format(self.apm_server_monitor_port)] + command_args,
            depends_on=self.depends_on,
            healthcheck=curl_healthcheck(self.SERVICE_PORT),
            labels=["co.elatic.apm.stack-version=" + self.version],
            ports=[
                self.publish_port(self.port, self.SERVICE_PORT),
                self.publish_port(self.apm_server_monitor_port, self.DEFAULT_MONITOR_PORT),
            ]
        )

        if self.build:
            build_spec_parts = self.build.split("@", 1)
            repo = build_spec_parts[0]
            branch = build_spec_parts[1] if len(build_spec_parts) > 1 else "master"
            content.update({
                "build": {
                    "context": "docker/apm-server",
                    "args": {
                        "apm_server_base_image": self.default_image(),
                        "apm_server_branch": branch,
                        "apm_server_repo": repo,
                    }
                },
                "image": None,
            })

        return content

    @staticmethod
    def enabled():
        return True

    def render(self):
        """hack up render to support multiple apm servers behind a load balancer"""
        ren = super(ApmServer, self).render()
        if self.apm_server_count == 1:
            return ren

        # save a single server for use as backend template
        single = ren[self.name()]
        single["ports"] = [p.rsplit(":", 1)[-1] for p in single["ports"]]

        # render proxy + backends
        ren = self.render_proxy()
        # individualize each backend instance
        for i in range(1, self.apm_server_count+1):
            backend = dict(single)
            backend["container_name"] = backend["container_name"] + "-" + str(i)
            ren.update({"-".join([self.name(), str(i)]): backend})
        return ren

    def render_proxy(self):
        condition = {"condition": "service_healthy"}
        content = dict(
            build={"context": "docker/apm-server/haproxy"},
            container_name=self.default_container_name() + "-load-balancer",
            depends_on={"apm-server-{}".format(i): condition for i in range(1, self.apm_server_count + 1)},
            environment={"APM_SERVER_COUNT": self.apm_server_count},
            healthcheck={"test": ["CMD", "haproxy", "-c", "-f", "/usr/local/etc/haproxy/haproxy.cfg"]},
            ports=[
                self.publish_port(self.port, self.SERVICE_PORT),
            ],
        )
        return {self.name(): content}


class Elasticsearch(StackService, Service):
    default_environment = ["cluster.name=docker-cluster", "bootstrap.memory_lock=true", "discovery.type=single-node"]
    default_es_java_opts = {
        "-Xms": "1g",
        "-Xmx": "1g",
    }

    SERVICE_PORT = 9200

    def __init__(self, **options):
        super(Elasticsearch, self).__init__(**options)
        if not self.oss and not self.at_least_version("6.3"):
            self.docker_name = self.name() + "-platinum"

        # construct elasticsearch environment variables
        # TODO: add command line option for java options (gr)
        es_java_opts = dict(self.default_es_java_opts)
        if self.at_least_version("6.4"):
            # per https://github.com/elastic/elasticsearch/pull/32138/files
            es_java_opts["-XX:UseAVX"] = "=2"

        java_opts_env = "ES_JAVA_OPTS=" + " ".join(["{}{}".format(k, v) for k, v in es_java_opts.items()])
        self.environment = self.default_environment + [
                java_opts_env, "path.data=/usr/share/elasticsearch/data/" + self.version]
        if not self.oss:
            self.environment.append("xpack.security.enabled=false")
            self.environment.append("xpack.license.self_generated.type=trial")
            if self.at_least_version("6.3"):
                self.environment.append("xpack.monitoring.collection.enabled=true")

    def _content(self):
        return dict(
            environment=self.environment,
            healthcheck={
                "interval": "20",
                "retries": 10,
                "test": ["CMD-SHELL", "curl -s http://localhost:9200/_cluster/health | grep -vq '\"status\":\"red\"'"],
            },
            mem_limit="5g",
            ports=[self.publish_port(self.port, self.SERVICE_PORT)],
            ulimits={
                "memlock": {"hard": -1, "soft": -1},
            },
            volumes=["esdata:/usr/share/elasticsearch/data"]
        )

    @staticmethod
    def enabled():
        return True


class BeatMixin(object):
    def __init__(self, **options):
        self.command = self.DEFAULT_COMMAND
        self.depends_on = {"elasticsearch": {"condition": "service_healthy"}}
        if options.get("enable_kibana", True):
            self.command += " -E setup.dashboards.enabled=true"
            self.depends_on["kibana"] = {"condition": "service_healthy"}
        super(BeatMixin, self).__init__(**options)


class Filebeat(BeatMixin, StackService, Service):
    DEFAULT_COMMAND = "filebeat -e --strict.perms=false"
    docker_path = "beats"

    def __init__(self, **options):
        super(Filebeat, self).__init__(**options)
        config = "filebeat.yml" if self.at_least_version("6.1") else "filebeat.simple.yml"
        self.filebeat_config_path = os.path.join(".", "docker", "filebeat", config)

    def _content(self):
        return dict(
            command=self.command,
            depends_on=self.depends_on,
            labels=None,
            user="root",
            volumes=[
                self.filebeat_config_path + ":/usr/share/filebeat/filebeat.yml",
                "/var/lib/docker/containers:/var/lib/docker/containers",
                "/var/run/docker.sock:/var/run/docker.sock",
            ]
        )


class Kibana(StackService, Service):
    default_environment = {"SERVER_NAME": "kibana.example.org", "ELASTICSEARCH_URL": "http://elasticsearch:9200"}

    SERVICE_PORT = 5601

    def __init__(self, **options):
        super(Kibana, self).__init__(**options)
        if not self.at_least_version("6.3") and not self.oss:
            self.docker_name = self.name() + "-x-pack"
        self.environment = self.default_environment.copy()
        if not self.oss:
            self.environment["XPACK_MONITORING_ENABLED"] = "true"
            if self.at_least_version("6.3"):
                self.environment["XPACK_XPACK_MAIN_TELEMETRY_ENABLED"] = "false"

    def _content(self):
        return dict(
            healthcheck=curl_healthcheck(self.SERVICE_PORT, "kibana", path="/api/status", interval="5s", retries=20),
            depends_on={"elasticsearch": {"condition": "service_healthy"}},
            environment=self.environment,
            ports=[self.publish_port(self.port, self.SERVICE_PORT)],
        )

    @staticmethod
    def enabled():
        return True


class Logstash(StackService, Service):
    SERVICE_PORT = 5044

    def _content(self):
        return dict(
            depends_on={"elasticsearch": {"condition": "service_healthy"}},
            environment={"ELASTICSEARCH_URL": "http://elasticsearch:9200"},
            healthcheck=curl_healthcheck(9600, "logstash", path="/"),
            ports=[self.publish_port(self.port, self.SERVICE_PORT), "9600"],
            volumes=["./docker/logstash/pipeline/:/usr/share/logstash/pipeline/"]
        )


class Metricbeat(BeatMixin, StackService, Service):
    DEFAULT_COMMAND = "metricbeat -e --strict.perms=false"
    docker_path = "beats"

    def _content(self):
        return dict(
            command=self.command,
            depends_on=self.depends_on,
            labels=None,
            user="root",
            volumes=[
                "./docker/metricbeat/metricbeat.yml:/usr/share/metricbeat/metricbeat.yml",
                "/var/run/docker.sock:/var/run/docker.sock",
            ]
        )


#
# Supporting Services
#
class Kafka(Service):
    SERVICE_PORT = 9092

    def _content(self):
        return dict(
            depends_on=["zookeeper"],
            environment={
                "KAFKA_ADVERTISED_LISTENERS": "PLAINTEXT://kafka:9092",
                "KAFKA_BROKER_ID": 1,
                "KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR": 1,
                "KAFKA_ZOOKEEPER_CONNECT": "zookeeper:2181",
            },
            image="confluentinc/cp-kafka:4.1.0",
            labels=None,
            logging=None,
            ports=[self.publish_port(self.port, self.SERVICE_PORT)],
        )


class Postgres(Service):
    SERVICE_PORT = 5432
    opbeans_side_car = True

    def _content(self):
        return dict(
            environment=["POSTGRES_DB=opbeans", "POSTGRES_PASSWORD=verysecure"],
            healthcheck={"interval": "10s", "test": ["CMD", "pg_isready", "-h", "postgres", "-U", "postgres"]},
            image="postgres:10",
            labels=None,
            ports=[self.publish_port(self.port, self.SERVICE_PORT, expose=True)],
            volumes=["./docker/opbeans/sql:/docker-entrypoint-initdb.d", "pgdata:/var/lib/postgresql/data"],

        )


class Redis(Service):
    SERVICE_PORT = 6379
    opbeans_side_car = True

    def _content(self):
        return dict(
            healthcheck={"interval": "10s", "test": ["CMD", "redis-cli", "ping"]},
            image="redis:4",
            labels=None,
            ports=[self.publish_port(self.port, self.SERVICE_PORT, expose=True)],
        )


class Zookeeper(Service):
    SERVICE_PORT = 2181

    def _content(self):
        return dict(
            environment={
                "ZOOKEEPER_CLIENT_PORT": 2181,
                "ZOOKEEPER_TICK_TIME": 2000,
            },
            image="confluentinc/cp-zookeeper:latest",
            labels=None,
            logging=None,
            ports=[self.publish_port(self.port, self.SERVICE_PORT)],
        )


#
# Agent Integration Test Services
#
class AgentRUMJS(Service):
    SERVICE_PORT = 8000
    DEFAULT_AGENT_BRANCH = "master"
    DEFAULT_AGENT_REPO = "elastic/apm-agent-js-base"

    def __init__(self, **options):
        super(AgentRUMJS, self).__init__(**options)
        self.agent_branch = options.get("rum_agent_branch", self.DEFAULT_AGENT_BRANCH)
        self.agent_repo = options.get("rum_agent_repo", self.DEFAULT_AGENT_REPO)

    @classmethod
    def add_arguments(cls, parser):
        super(AgentRUMJS, cls).add_arguments(parser)
        parser.add_argument(
            '--rum-agent-repo',
            default=cls.DEFAULT_AGENT_REPO,
        )
        parser.add_argument(
            '--rum-agent-branch',
            default=cls.DEFAULT_AGENT_BRANCH,
        )

    def _content(self):
        return dict(
            build=dict(
                context="docker/rum",
                dockerfile="Dockerfile",
                args=[
                    "RUM_AGENT_BRANCH=" + self.agent_branch,
                    "RUM_AGENT_REPO=" + self.agent_repo,
                ]
            ),
            container_name="rum",
            image=None,
            labels=None,
            logging=None,
            environment={
                "ELASTIC_APM_SERVICE_NAME": "rum",
                "ELASTIC_APM_SERVER_URL": "http://apm-server:8200"
            },
            ports=[self.publish_port(self.port, self.SERVICE_PORT)],
        )


class AgentGoNetHttp(Service):
    SERVICE_PORT = 8080

    def _content(self):
        return dict(
            build={"context": "docker/go/nethttp", "dockerfile": "Dockerfile"},
            container_name="gonethttpapp",
            environment={
                "ELASTIC_APM_SERVICE_NAME": "gonethttpapp",
                "ELASTIC_APM_SERVER_URL": "http://apm-server:8200",
                "ELASTIC_APM_TRANSACTION_IGNORE_NAMES": "healthcheck",
                "ELASTIC_APM_FLUSH_INTERVAL": "500ms",
            },
            healthcheck=curl_healthcheck(self.SERVICE_PORT, "gonethttpapp"),
            image=None,
            labels=None,
            logging=None,
            ports=[self.publish_port(self.port, self.SERVICE_PORT)],
        )


class AgentNodejsExpress(Service):
    # elastic/apm-agent-nodejs#master
    DEFAULT_AGENT_PACKAGE = "elastic-apm-node"
    SERVICE_PORT = 8010

    def __init__(self, **options):
        super(AgentNodejsExpress, self).__init__(**options)
        self.agent_package = options.get("nodejs_agent_package", self.DEFAULT_AGENT_PACKAGE)

    @classmethod
    def add_arguments(cls, parser):
        super(AgentNodejsExpress, cls).add_arguments(parser)
        parser.add_argument(
            '--nodejs-agent-package',
            default=cls.DEFAULT_AGENT_PACKAGE,
        )

    def _content(self):
        return dict(
            build={"context": "docker/nodejs/express", "dockerfile": "Dockerfile"},
            command="bash -c \"npm install {} && node app.js\"".format(
                self.agent_package, self.SERVICE_PORT),
            container_name="expressapp",
            healthcheck=curl_healthcheck(self.SERVICE_PORT, "expressapp"),
            image=None,
            labels=None,
            logging=None,
            environment={
                "APM_SERVER_URL": "http://apm-server:8200",
                "EXPRESS_PORT": str(self.SERVICE_PORT),
                "EXPRESS_SERVICE_NAME": "expressapp",
            },
            ports=[self.publish_port(self.port, self.SERVICE_PORT)],
        )


class AgentPython(Service):
    DEFAULT_AGENT_PACKAGE = "elastic-apm"
    _arguments_added = False

    def __init__(self, **options):
        super(AgentPython, self).__init__(**options)
        self.agent_package = options.get("python_agent_package", self.DEFAULT_AGENT_PACKAGE)

    @classmethod
    def add_arguments(cls, parser):
        if cls._arguments_added:
            return

        super(AgentPython, cls).add_arguments(parser)
        parser.add_argument(
            '--python-agent-package',
            default=cls.DEFAULT_AGENT_PACKAGE,
        )
        # prevent calling again
        cls._arguments_added = True

    def _content(self):
        raise NotImplementedError()


class AgentPythonDjango(AgentPython):
    SERVICE_PORT = 8003

    def _content(self):
        return dict(
            build={"context": "docker/python/django", "dockerfile": "Dockerfile"},
            command="bash -c \"pip install -U {} && python testapp/manage.py runserver 0.0.0.0:{}\"".format(
                self.agent_package, self.SERVICE_PORT),
            container_name="djangoapp",
            environment={
                "APM_SERVER_URL": "http://apm-server:8200",
                "DJANGO_PORT": self.SERVICE_PORT,
                "DJANGO_SERVICE_NAME": "djangoapp",
            },
            healthcheck=curl_healthcheck(self.SERVICE_PORT, "djangoapp"),
            image=None,
            labels=None,
            logging=None,
            ports=[self.publish_port(self.port, self.SERVICE_PORT)],
        )


class AgentPythonFlask(AgentPython):
    SERVICE_PORT = 8001

    def _content(self):
        return dict(
            build={"context": "docker/python/flask", "dockerfile": "Dockerfile"},
            command="bash -c \"pip install -U {} && gunicorn app:app\"".format(self.agent_package),
            container_name="flaskapp",
            image=None,
            labels=None,
            logging=None,
            environment={
                "APM_SERVER_URL": "http://apm-server:8200",
                "FLASK_SERVICE_NAME": "flaskapp",
                "GUNICORN_CMD_ARGS": "-w 4 -b 0.0.0.0:{}".format(self.SERVICE_PORT),
            },
            healthcheck=curl_healthcheck(self.SERVICE_PORT, "flaskapp"),
            ports=[self.publish_port(self.port, self.SERVICE_PORT)],
        )


class AgentRubyRails(Service):
    DEFAULT_AGENT_VERSION = "latest"
    DEFAULT_AGENT_VERSION_STATE = "release"
    SERVICE_PORT = 8020

    @classmethod
    def add_arguments(cls, parser):
        super(AgentRubyRails, cls).add_arguments(parser)
        parser.add_argument(
            "--ruby-agent-version",
            default=cls.DEFAULT_AGENT_VERSION,
        )
        parser.add_argument(
            "--ruby-agent-version-state",
            default=cls.DEFAULT_AGENT_VERSION_STATE,
        )

    def __init__(self, **options):
        super(AgentRubyRails, self).__init__(**options)
        self.agent_version = options.get("ruby_agent_version", self.DEFAULT_AGENT_VERSION)
        self.agent_version_state = options.get("ruby_agent_version_state", self.DEFAULT_AGENT_VERSION_STATE)

    def _content(self):
        return dict(
            build={"context": "docker/ruby/rails", "dockerfile": "Dockerfile"},
            command="bash -c \"bundle install && RAILS_ENV=production bundle exec rails s -b 0.0.0.0 -p {}\"".format(
                self.SERVICE_PORT),
            container_name="railsapp",
            environment={
                "APM_SERVER_URL": "http://apm-server:8200",
                "ELASTIC_APM_SERVER_URL": "http://apm-server:8200",
                "ELASTIC_APM_SERVICE_NAME": "railsapp",
                "RAILS_PORT": self.SERVICE_PORT,
                "RAILS_SERVICE_NAME": "railsapp",
                "RUBY_AGENT_VERSION_STATE": self.agent_version_state,
                "RUBY_AGENT_VERSION": self.agent_version,
            },
            healthcheck=curl_healthcheck(self.SERVICE_PORT, "railsapp", interval="10s", retries=60),
            image=None,
            labels=None,
            logging=None,
            ports=[self.publish_port(self.port, self.SERVICE_PORT)],
        )


class AgentJavaSpring(Service):
    SERVICE_PORT = 8090

    def _content(self):
        return dict(
            build={"context": "docker/java/spring", "dockerfile": "Dockerfile"},
            container_name="javaspring",
            image=None,
            labels=None,
            logging=None,
            environment={
                "ELASTIC_APM_SERVICE_NAME": "springapp",
                "ELASTIC_APM_SERVER_URL": "http://apm-server:8200",
            },
            healthcheck=curl_healthcheck(self.SERVICE_PORT, "javaspring"),
            ports=[self.publish_port(self.port, self.SERVICE_PORT)],
        )

#
# Opbeans Services
#


class OpbeansService(Service):
    DEFAULT_APM_SERVER_URL = "http://apm-server:8200"
    DEFAULT_APM_JS_SERVER_URL = "http://apm-server:8200"

    def __init__(self, **options):
        super(OpbeansService, self).__init__(**options)
        self.apm_server_url = options.get("opbeans_apm_server_url", self.DEFAULT_APM_SERVER_URL)
        self.apm_js_server_url = options.get("opbeans_apm_js_server_url", self.DEFAULT_APM_JS_SERVER_URL)

    @classmethod
    def add_arguments(cls, parser):
        """add service-specific command line arguments"""
        # allow port overrides
        super(OpbeansService, cls).add_arguments(parser)
        if hasattr(cls, 'DEFAULT_SERVICE_NAME'):
            parser.add_argument(
                '--' + cls.name() + '-service-name',
                default=cls.DEFAULT_SERVICE_NAME,
                dest=cls.option_name() + '_service_name',
                help="service name"
            )


class OpbeansGo(OpbeansService):
    SERVICE_PORT = 3003
    DEFAULT_AGENT_BRANCH = "master"
    DEFAULT_AGENT_REPO = "elastic/apm-agent-go"

    def __init__(self, **options):
        super(OpbeansGo, self).__init__(**options)
        self.agent_branch = options.get("opbeans_agent_branch", self.DEFAULT_AGENT_BRANCH)
        self.agent_repo = options.get("opbeans_agent_repo", self.DEFAULT_AGENT_REPO)

    def _content(self):
        depends_on = {
            "elasticsearch": {"condition": "service_healthy"},
            "postgres": {"condition": "service_healthy"},
            "redis": {"condition": "service_healthy"},
        }

        if self.options.get("enable_apm_server", True):
            depends_on["apm-server"] = {"condition": "service_healthy"}

        content = dict(
            build=dict(
                context="docker/opbeans/go",
                dockerfile="Dockerfile",
                args=[
                    "GO_AGENT_BRANCH=" + self.agent_branch,
                    "GO_AGENT_REPO=" + self.agent_repo,
                ]
            ),
            environment=[
                "ELASTIC_APM_SERVER_URL={}".format(self.apm_server_url),
                "ELASTIC_APM_JS_SERVER_URL={}".format(self.apm_js_server_url),
                "ELASTIC_APM_FLUSH_INTERVAL=5",
                "ELASTIC_APM_TRANSACTION_MAX_SPANS=50",
                "ELASTIC_APM_SAMPLE_RATE=1",
                "ELASTICSEARCH_URL=http://elasticsearch:9200",
                "OPBEANS_CACHE=redis://redis:6379",
                "OPBEANS_PORT=3000",
                "PGHOST=postgres",
                "PGPORT=5432",
                "PGUSER=postgres",
                "PGPASSWORD=verysecure",
                "PGSSLMODE=disable",
            ],
            depends_on=depends_on,
            image=None,
            labels=None,
            ports=[self.publish_port(self.port, 3000)],
        )
        return content


class OpbeansJava(OpbeansService):
    SERVICE_PORT = 3002
    DEFAULT_AGENT_BRANCH = "master"
    DEFAULT_AGENT_REPO = "elastic/apm-agent-java"
    DEFAULT_LOCAL_REPO = "."
    DEFAULT_SERVICE_NAME = 'opbeans-java'

    @classmethod
    def add_arguments(cls, parser):
        super(OpbeansJava, cls).add_arguments(parser)
        parser.add_argument(
            '--opbeans-java-local-repo',
            default=cls.DEFAULT_LOCAL_REPO,
        )

    def __init__(self, **options):
        super(OpbeansJava, self).__init__(**options)
        self.local_repo = options.get("opbeans_java_local_repo", self.DEFAULT_LOCAL_REPO)
        self.agent_branch = options.get("opbeans_agent_branch", self.DEFAULT_AGENT_BRANCH)
        self.agent_repo = options.get("opbeans_agent_repo", self.DEFAULT_AGENT_REPO)
        self.service_name = options.get("opbeans_java_service_name", self.DEFAULT_SERVICE_NAME)

    def _content(self):
        depends_on = {
            "elasticsearch": {"condition": "service_healthy"},
            "postgres": {"condition": "service_healthy"},
        }

        if self.options.get("enable_apm_server", True):
            depends_on["apm-server"] = {"condition": "service_healthy"}

        content = dict(
            build=dict(
                context="docker/opbeans/java",
                dockerfile="Dockerfile",
                args=[
                    "JAVA_AGENT_BRANCH=" + self.agent_branch,
                    "JAVA_AGENT_REPO=" + self.agent_repo,
                ]
            ),
            environment=[
                "ELASTIC_APM_SERVICE_NAME={}".format(self.service_name),
                "ELASTIC_APM_APPLICATION_PACKAGES=co.elastic.apm.opbeans",
                "ELASTIC_APM_SERVER_URL={}".format(self.apm_server_url),
                "ELASTIC_APM_FLUSH_INTERVAL=5",
                "ELASTIC_APM_TRANSACTION_MAX_SPANS=50",
                "ELASTIC_APM_SAMPLE_RATE=1",
                "DATABASE_URL=jdbc:postgresql://postgres/opbeans?user=postgres&password=verysecure",
                "DATABASE_DIALECT=POSTGRESQL",
                "DATABASE_DRIVER=org.postgresql.Driver",
                "REDIS_URL=redis://redis:6379",
                "ELASTICSEARCH_URL=http://elasticsearch:9200",
                "OPBEANS_SERVER_PORT=3000",
                "JAVA_AGENT_VERSION",
            ],
            depends_on=depends_on,
            image=None,
            labels=None,
            healthcheck=curl_healthcheck(3000, "opbeans-java", path="/"),
            ports=[self.publish_port(self.port, 3000)],
            volumes=[
                "{}:/local-install".format(self.local_repo),
            ]
        )
        return content


class OpbeansNode(OpbeansService):
    SERVICE_PORT = 3000
    DEFAULT_LOCAL_REPO = "."
    DEFAULT_SERVICE_NAME = "opbeans-node"

    @classmethod
    def add_arguments(cls, parser):
        super(OpbeansNode, cls).add_arguments(parser)
        parser.add_argument(
            '--opbeans-node-local-repo',
            default=cls.DEFAULT_LOCAL_REPO,
        )

    def __init__(self, **options):
        super(OpbeansNode, self).__init__(**options)
        self.local_repo = options.get("opbeans_node_local_repo", self.DEFAULT_LOCAL_REPO)
        self.service_name = options.get("opbeans_node_service_name", self.DEFAULT_SERVICE_NAME)

    def _content(self):
        depends_on = {
            "postgres": {"condition": "service_healthy"},
            "redis": {"condition": "service_healthy"},
        }

        if self.options.get("enable_apm_server", True):
            depends_on["apm-server"] = {"condition": "service_healthy"}

        content = dict(
            build={"context": "docker/opbeans/node", "dockerfile": "Dockerfile"},
            environment=[
                "ELASTIC_APM_SERVER_URL={}".format(self.apm_server_url),
                "ELASTIC_APM_JS_SERVER_URL={}".format(self.apm_js_server_url),
                "ELASTIC_APM_APP_NAME=opbeans-node",
                "ELASTIC_APM_SERVICE_NAME={}".format(self.service_name),
                "ELASTIC_APM_LOG_LEVEL=info",
                "ELASTIC_APM_SOURCE_LINES_ERROR_APP_FRAMES",
                "ELASTIC_APM_SOURCE_LINES_SPAN_APP_FRAMES=5",
                "ELASTIC_APM_SOURCE_LINES_ERROR_LIBRARY_FRAMES",
                "ELASTIC_APM_SOURCE_LINES_SPAN_LIBRARY_FRAMES",
                "WORKLOAD_ELASTIC_APM_APP_NAME=workload",
                "WORKLOAD_ELASTIC_APM_SERVER_URL={}".format(self.apm_server_url),
                "OPBEANS_SERVER_PORT=3000",
                "OPBEANS_SERVER_HOSTNAME=opbeans-node",
                "NODE_ENV=production",
                "PGHOST=postgres",
                "PGPASSWORD=verysecure",
                "PGPORT=5432",
                "PGUSER=postgres",
                "REDIS_URL=redis://redis:6379",
                "NODE_AGENT_BRANCH=1.x",
            ],
            depends_on=depends_on,
            image=None,
            labels=None,
            healthcheck=curl_healthcheck(3000, "opbeans-node", path="/"),
            ports=[self.publish_port(self.port, 3000)],
            volumes=[
                "{}:/local-install".format(self.local_repo),
                "./docker/opbeans/node/sourcemaps:/sourcemaps",
            ]
        )
        return content


class OpbeansPython(OpbeansService):
    SERVICE_PORT = 8000
    DEFAULT_AGENT_REPO = "elastic/apm-agent-python"
    DEFAULT_AGENT_BRANCH = "2.x"
    DEFAULT_LOCAL_REPO = "."
    DEFAULT_SERVICE_NAME = 'opbeans-python'

    @classmethod
    def add_arguments(cls, parser):
        super(OpbeansPython, cls).add_arguments(parser)
        parser.add_argument(
            '--opbeans-python-local-repo',
            default=cls.DEFAULT_LOCAL_REPO,
        )

    def __init__(self, **options):
        super(OpbeansPython, self).__init__(**options)
        self.local_repo = options.get("opbeans_python_local_repo", self.DEFAULT_LOCAL_REPO)
        if self.version.split(".", 3)[0:2] < ["6", "2"]:
            self.agent_branch = "1.x"
        else:
            self.agent_branch = self.DEFAULT_AGENT_BRANCH
        self.agent_repo = options.get("opbeans_agent_repo", self.DEFAULT_AGENT_REPO)
        self.service_name = options.get("opbeans_python_service_name", self.DEFAULT_SERVICE_NAME)

    def _content(self):
        depends_on = {
            "elasticsearch": {"condition": "service_healthy"},
            "postgres": {"condition": "service_healthy"},
            "redis": {"condition": "service_healthy"},
        }

        if self.options.get("enable_apm_server", True):
            depends_on["apm-server"] = {"condition": "service_healthy"}

        content = dict(
            build={"context": "docker/opbeans/python", "dockerfile": "Dockerfile"},
            environment=[
                "DATABASE_URL=postgres://postgres:verysecure@postgres/opbeans",
                "ELASTIC_APM_SERVICE_NAME={}".format(self.service_name),
                "ELASTIC_APM_SERVER_URL={}".format(self.apm_server_url),
                "ELASTIC_APM_JS_SERVER_URL={}".format(self.apm_js_server_url),
                "ELASTIC_APM_FLUSH_INTERVAL=5",
                "ELASTIC_APM_TRANSACTION_MAX_SPANS=50",
                "ELASTIC_APM_TRANSACTION_SAMPLE_RATE=0.5",
                "ELASTIC_APM_SOURCE_LINES_ERROR_APP_FRAMES",
                "ELASTIC_APM_SOURCE_LINES_SPAN_APP_FRAMES=5",
                "ELASTIC_APM_SOURCE_LINES_ERROR_LIBRARY_FRAMES",
                "ELASTIC_APM_SOURCE_LINES_SPAN_LIBRARY_FRAMES",
                "REDIS_URL=redis://redis:6379",
                "ELASTICSEARCH_URL=http://elasticsearch:9200",
                "OPBEANS_SERVER_URL=http://opbeans-python:3000",
                "PYTHON_AGENT_BRANCH=" + self.agent_branch,
                "PYTHON_AGENT_REPO=" + self.agent_repo,
                "PYTHON_AGENT_VERSION",
            ],
            depends_on=depends_on,
            image=None,
            labels=None,
            healthcheck=curl_healthcheck(3000, "opbeans-python", path="/"),
            ports=[self.publish_port(self.port, 3000)],
            volumes=[
                "{}:/local-install".format(self.local_repo),
            ]
        )
        return content


class OpbeansRuby(OpbeansService):
    SERVICE_PORT = 3001
    DEFAULT_AGENT_BRANCH = "master"
    DEFAULT_AGENT_REPO = "elastic/apm-agent-ruby"
    DEFAULT_LOCAL_REPO = "."
    DEFAULT_SERVICE_NAME = "opbeans-ruby"

    @classmethod
    def add_arguments(cls, parser):
        super(OpbeansRuby, cls).add_arguments(parser)
        parser.add_argument(
            '--opbeans-ruby-local-repo',
            default=cls.DEFAULT_LOCAL_REPO,
        )

    def __init__(self, **options):
        super(OpbeansRuby, self).__init__(**options)
        self.local_repo = options.get("opbeans_ruby_local_repo", self.DEFAULT_LOCAL_REPO)
        self.agent_branch = options.get("opbeans_agent_branch", self.DEFAULT_AGENT_BRANCH)
        self.agent_repo = options.get("opbeans_agent_repo", self.DEFAULT_AGENT_REPO)
        self.service_name = options.get("opbeans_ruby_service_name", self.DEFAULT_SERVICE_NAME)

    def _content(self):
        depends_on = {
            "elasticsearch": {"condition": "service_healthy"},
            "postgres": {"condition": "service_healthy"},
            "redis": {"condition": "service_healthy"},
        }

        if self.options.get("enable_apm_server", True):
            depends_on["apm-server"] = {"condition": "service_healthy"}

        content = dict(
            build={"context": "docker/opbeans/ruby", "dockerfile": "Dockerfile"},
            environment=[
                "ELASTIC_APM_SERVER_URL={}".format(self.apm_server_url),
                "ELASTIC_APM_SERVICE_NAME={}".format(self.service_name),
                "DATABASE_URL=postgres://postgres:verysecure@postgres/opbeans-ruby",
                "REDIS_URL=redis://redis:6379",
                "ELASTICSEARCH_URL=http://elasticsearch:9200",
                "OPBEANS_SERVER_URL=http://opbeans-ruby:3000",
                "RAILS_ENV=production",
                "RAILS_LOG_TO_STDOUT=1",
                "PORT=3000",
                "RUBY_AGENT_BRANCH=" + self.agent_branch,
                "RUBY_AGENT_REPO=" + self.agent_repo,
                "RUBY_AGENT_VERSION",
            ],
            depends_on=depends_on,
            image=None,
            labels=None,
            # lots of retries as the ruby app can take a long time to boot
            healthcheck=curl_healthcheck(3000, "opbeans-ruby", path="/", retries=100),
            ports=[self.publish_port(self.port, 3000)],
            volumes=[
                "{}:/local-install".format(self.local_repo),
            ]
        )
        return content


class OpbeansRum(Service):
    # OpbeansRum is not really an Opbeans service, so we inherit from Service
    SERVICE_PORT = 9222

    @classmethod
    def add_arguments(cls, parser):
        super(OpbeansRum, cls).add_arguments(parser)
        parser.add_argument(
            '--opbeans-rum-backend-service',
            default='opbeans-node',
        )
        parser.add_argument(
            '--opbeans-rum-backend-port',
            default='3000',
        )

    def __init__(self, **options):
        super(OpbeansRum, self).__init__(**options)
        self.backend_service = options.get('opbeans_rum_backend_service', 'opbeans-node')
        self.backend_port = options.get('opbeans_rum_backend_port', '3000')

    def _content(self):
        content = dict(
            build={"context": "docker/opbeans/rum", "dockerfile": "Dockerfile"},
            cap_add=["SYS_ADMIN"],
            depends_on={self.backend_service: {'condition': 'service_healthy'}},
            environment=[
                "OPBEANS_BASE_URL=http://{}:{}".format(self.backend_service, self.backend_port),
            ],
            image=None,
            labels=None,
            healthcheck=curl_healthcheck(self.SERVICE_PORT, "opbeans-rum", path="/"),
            ports=[self.publish_port(self.port, self.SERVICE_PORT)],
        )
        return content


class OpbeansLoadGenerator(Service):
    opbeans_side_car = True

    @classmethod
    def add_arguments(cls, parser):
        super(OpbeansLoadGenerator, cls).add_arguments(parser)
        for service_class in OpbeansService.__subclasses__():
            parser.add_argument(
                '--no-%s-loadgen' % service_class.name(),
                action='store_true',
                default=False,
                help='Disable load generator for {}'.format(service_class.name())
            )
            parser.add_argument(
                '--%s-loadgen-rpm' % service_class.name(),
                action='store',
                default=100,
                help='RPM of load that should be generated for {}'.format(service_class.name())
            )

    def __init__(self, **options):
        super(OpbeansLoadGenerator, self).__init__(**options)
        self.loadgen_services = []
        self.loadgen_rpms = {}
        # create load for opbeans services
        run_all_opbeans = options.get('run_all_opbeans')
        excluded = ('opbeans_load_generator', 'opbeans_rum', 'opbeans_node')
        for flag, value in options.items():
            if (value or run_all_opbeans) and flag.startswith('enable_opbeans_'):
                service_name = flag[len('enable_'):]
                if not options.get('no_{}_loadgen'.format(service_name)) and service_name not in excluded:
                    self.loadgen_services.append(service_name.replace('_', '-'))
                    rpm = options.get('{}_loadgen_rpm'.format(service_name))
                    if rpm:
                        self.loadgen_rpms[service_name.replace('_', '-')] = rpm

    def _content(self):
        content = dict(
            image="opbeans/opbeans-loadgen:latest",
            depends_on={service: {'condition': 'service_healthy'} for service in self.loadgen_services},
            environment=[
                "OPBEANS_URLS={}".format(','.join('{0}:http://{0}:3000'.format(s) for s in self.loadgen_services)),
                "OPBEANS_RPMS={}".format(','.join('{}:{}'.format(k, v) for k, v in self.loadgen_rpms.items()))
            ],
            labels=None,
        )
        return content


#
# Service Tests
#

class LocalSetup(object):
    SUPPORTED_VERSIONS = {
        '6.0': '6.0.1',
        '6.1': '6.1.3',
        '6.2': '6.2.4',
        '6.3': '6.3.3',
        '6.4': '6.4.0',
        '6.5': '6.5.0',
        'master': '7.0.0-alpha1'
    }

    def __init__(self, argv=None, services=None):
        self.available_options = set()

        if services is None:
            services = discover_services()
        self.services = services

        parser = argparse.ArgumentParser(
            description="""
            This is a CLI for managing the local testing stack.
            Read the README.md for more information.
            """
        )

        # Add script version
        parser.add_argument(
            '-v',
            action='version',
            version='{0} v{1}'.format(PACKAGE_NAME, __version__)
        )

        # Add debug mode
        parser.add_argument(
            '--debug',
            help="Start in debug mode (more verbose)",
            action="store_const",
            dest="loglevel",
            const=logging.DEBUG,
            default=logging.INFO
        )

        subparsers = parser.add_subparsers(
            title='subcommands',
            description='Use one of the following commands:'
        )

        self.init_start_parser(
            subparsers.add_parser(
                'start',
                help="Start the stack. See `start --help` for options.",
                description="Main command for this script, starts the stack. Use the arguments to specify which "
                            "services to start. "
            ),
            services,
            argv=argv,
        ).set_defaults(func=self.start_handler)

        subparsers.add_parser(
            'status',
            help="Prints status of all services.",
            description="Prints the container status for each running service."
        ).set_defaults(func=self.status_handler)

        subparsers.add_parser(
            'load-dashboards',
            help="Loads APM dashbords into Kibana using APM Server.",
            description="Loads APM dashbords into Kibana using APM Server. APM Server, Elasticsearch, and Kibana must "
                        "be running. "
        ).set_defaults(func=self.dashboards_handler)

        subparsers.add_parser(
            'versions',
            help="Prints all running version numbers.",
            description="Prints version (and build) numbers of each running service."
        ).set_defaults(func=self.versions_handler)

        subparsers.add_parser(
            'stop',
            help="Stops all services.",
            description="Stops all running services and their containers."
        ).set_defaults(func=self.stop_handler)

        subparsers.add_parser(
            'list-options',
            help="Lists all available options.",
            description="Lists all available options (used for bash autocompletion)."
        ).set_defaults(func=self.listoptions_handler)

        self.init_sourcemap_parser(
            subparsers.add_parser(
                'upload-sourcemap',
                help="Uploads sourcemap to the APM Server"
            )
        ).set_defaults(func=self.upload_sourcemaps_handler)

        self.store_options(parser)

        self.args = parser.parse_args(argv)

        # py3
        if not hasattr(self.args, "func"):
            parser.error("command required")

    def set_docker_compose_path(self, dst):
        """override docker-compose-path argument, for tests"""
        self.args.__setattr__("docker_compose_path", dst)

    def __call__(self):
        self.args.func()

    def init_start_parser(self, parser, services, argv=None):
        if not argv:
            argv = sys.argv
        available_versions = ' / '.join(list(self.SUPPORTED_VERSIONS))
        help_text = (
                "Which version of the stack to start. " +
                "Available options: {0}".format(available_versions)
        )
        parser.add_argument("stack-version", action='store', help=help_text)

        # Add a --no-x / --with-x argument for each service
        for service in services:
            if not service.opbeans_side_car:
                enabled_group = parser.add_mutually_exclusive_group()
                enabled_group.add_argument(
                    '--with-' + service.name(),
                    action='store_true',
                    dest='enable_' + service.option_name(),
                    help='Enable ' + service.name(),
                    default=service.enabled(),
                )

                enabled_group.add_argument(
                    '--no-' + service.name(),
                    action='store_false',
                    dest='enable_' + service.option_name(),
                    help='Disable ' + service.name(),
                    default=service.enabled(),
                )
            service.add_arguments(parser)

        # Add build candidate argument
        build_type_group = parser.add_mutually_exclusive_group()
        build_type_group.add_argument(
            '--bc',
            action='store',
            dest='bc',
            help='ID of the build candidate, e.g. 37b864a0',
            default=''
        )
        build_type_group.add_argument(
            '--release',
            action='store_true',
            dest='release',
            help='Use released version',
            default=False
        )
        build_type_group.add_argument(
            '--snapshot',
            action='store_false',
            dest='release',
            help='use snapshot version',
            default='',
        )

        # Add option to skip image downloads
        parser.add_argument(
            '--skip-download',
            action='store_true',
            dest='skip_download',
            help='Skip the download of fresh images and use current ones'
        )

        # option for path to docker-compose.yml
        parser.add_argument(
            '--docker-compose-path',
            type=argparse.FileType(mode='w'),
            default=os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'docker-compose.yml')),
            help='path to docker-compose.yml'
        )

        # option to add a service and keep the rest running
        parser.add_argument(
            '--append',
            action='store_true',
            dest='append-service',
            help='Do not stop running services'
        )

        # Add image cache arguments
        parser.add_argument(
            '--image-cache-dir',
            default=os.path.abspath(os.path.join(os.path.dirname(__file__), '.images')),
            help='image cache directory',
        )

        parser.add_argument(
            "--build-parallel",
            action="store_true",
            help="build images in parallel",
            dest="build_parallel",
            default=False,
        )

        parser.add_argument(
            '--force-build',
            action='store_true',
            help='force build of any images without docker cache',
            dest='force_build',
            default=False,
        )

        parser.add_argument(
            '--all',
            action='store_true',
            help='run all opbeans services',
            dest='run_all_opbeans',
            default=False,
        )

        parser.add_argument(
            '--oss',
            action='store_true',
            help='use oss container images',
            dest='oss',
            default=False,
        )

        parser.add_argument(
            '--opbeans-apm-server-url',
            action='store',
            help='server_url to use for Opbeans services',
            dest='opbeans_apm_server_url',
            default='http://apm-server:8200',
        )

        parser.add_argument(
            '--opbeans-apm-js-server-url',
            action='store',
            help='server_url to use for Opbeans frontend service',
            dest='opbeans_apm_js_server_url',
            default='http://apm-server:8200',
        )

        self.store_options(parser)

        return parser

    @staticmethod
    def init_sourcemap_parser(parser):
        parser.add_argument(
            '--sourcemap-file',
            action='store',
            dest='sourcemap_file',
            help='path to the sourcemap to upload. Defaults to first map found in node/sourcemaps directory',
            default=''
        )

        parser.add_argument(
            '--server-url',
            action='store',
            dest='server_url',
            help='URL of the apm-server. Defaults to running apm-server container, if any',
            default=''
        )

        parser.add_argument(
            '--service-name',
            action='store',
            dest='service_name',
            help='Name of the frontend app. Defaults to "opbeans-react"',
            default='opbeans-react'
        )

        parser.add_argument(
            '--service-version',
            action='store',
            dest='service_version',
            help='Version of the frontend app. Defaults to the BUILDDATE env variable of the "opbeans-node" container',
            default=''
        )

        parser.add_argument(
            '--bundle-path',
            action='store',
            dest='bundle_path',
            help='Bundle path in minified files. Defaults to "http://opbeans-node:3000/static/js/" + name of sourcemap',
            default=''
        )

        parser.add_argument(
            '--secret-token',
            action='store',
            dest='secret_token',
            help='Secret token to authenticate against the APM server. Empty by default.',
            default=''
        )

        return parser

    def store_options(self, parser):
        """
        Helper method to extract and store all arguments
        in a list of all possible arguments.
        Used for bash tab completion.
        """
        # Run through all parser actions
        for action in parser._actions:
            for option in action.option_strings:
                self.available_options.add(option)

        # Get subparsers from parser
        subparsers_actions = [
            action for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ]

        # Run through all subparser actions
        for subparsers_action in subparsers_actions:
            for choice, subparser in subparsers_action.choices.items():
                self.available_options.add(choice)

    #
    # handlers
    #
    @staticmethod
    def dashboards_handler():
        cmd = (
                'docker ps --filter "name=kibana" -q | xargs docker inspect ' +
                '-f \'{{ index .Config.Labels "co.elatic.apm.stack-version" }}\''
        )

        # Check if Docker is running and get running containers
        try:
            running_version = subprocess.check_output(cmd, shell=True).decode('utf8').strip()
        except subprocess.CalledProcessError:
            # If not, exit immediately
            print('Make sure Docker is running before running this script.')
            sys.exit(1)

        # Check for empty result
        if running_version == "":
            print('No containers are running.')
            print('Make sure the stack is running before importing dashboards.')
            sys.exit(1)

        # Prepare and call command
        print("Loading Kibana dashboards using APM Server:\n")
        cmd = (
                'docker-compose run --rm ' +
                'apm-server -e setup -E setup.kibana.host="kibana:5601"'
        )
        subprocess.call(cmd, shell=True)

    def listoptions_handler(self):
        print("{}".format(" ".join(self.available_options)))

    def start_handler(self):
        args = vars(self.args)

        if "version" not in args:
            # use stack-version directly if not supported, to allow use of specific releases, eg 6.2.3
            args["version"] = self.SUPPORTED_VERSIONS.get(args["stack-version"], args["stack-version"])

        selections = set()
        all_opbeans = args.get('run_all_opbeans')
        any_opbeans = all_opbeans or any(v and k.startswith('enable_opbeans_') for k, v in args.items())
        for service in self.services:
            service_enabled = args.get("enable_" + service.option_name())
            is_opbeans_service = issubclass(service, OpbeansService) or service is OpbeansRum
            is_opbeans_sidecar = service.name() in ('postgres', 'redis', 'opbeans-load-generator')
            if service_enabled or (all_opbeans and is_opbeans_service) or (any_opbeans and is_opbeans_sidecar):
                selections.add(service(**args))

        # `docker load` images if necessary, usually only for build candidates
        services_to_load = {}
        for service in selections:
            download_url = service.image_download_url()
            if download_url:
                services_to_load[service.name()] = download_url
        if not args["skip_download"] and services_to_load:
            load_images(set(services_to_load.values()), args["image_cache_dir"])

        # generate docker-compose.yml
        services = {}
        for service in selections:
            services.update(service.render())
        compose = dict(
            version="2.1",
            services=services,
            networks=dict(
                default={"name": "apm-integration-testing"},
            ),
            volumes=dict(
                esdata={"driver": "local"},
                pgdata={"driver": "local"},
            ),
        )
        docker_compose_path = args["docker_compose_path"]
        json.dump(compose, docker_compose_path, indent=2, sort_keys=True)
        docker_compose_path.flush()

        # try to figure out if writing to a real file, not amazing
        if hasattr(docker_compose_path, "name") and os.path.isdir(os.path.dirname(docker_compose_path.name)):
            docker_compose_path.close()
            print("Starting stack services..\n")

            # always build if possible, should be quick for rebuilds
            build_services = [name for name, service in compose["services"].items() if 'build' in service]
            if build_services:
                docker_compose_build = ["docker-compose", "-f", docker_compose_path.name, "build", "--pull"]
                if args["force_build"]:
                    docker_compose_build.append("--no-cache")
                if args["build_parallel"]:
                    docker_compose_build.append("--parallel")
                subprocess.call(docker_compose_build + build_services)

            # pull any images
            image_services = [name for name, service in compose["services"].items() if
                              'image' in service and name not in services_to_load]
            if image_services:
                subprocess.call(["docker-compose", "-f", docker_compose_path.name, "pull"] + image_services)
            # really start
            docker_compose_up = ["docker-compose", "-f", docker_compose_path.name, "up", "-d"]
            subprocess.call(docker_compose_up)

    @staticmethod
    def status_handler():
        print("Status for all services:\n")
        subprocess.call(['docker-compose', 'ps'])

    @staticmethod
    def stop_handler():
        print("Stopping all stack services..\n")
        subprocess.call(['docker-compose', 'stop'])

    def upload_sourcemaps_handler(self):
        server_url = self.args.server_url
        sourcemap_file = self.args.sourcemap_file
        bundle_path = self.args.bundle_path
        service_version = self.args.service_version
        if not server_url:
            cmd = 'docker ps --filter "name=apm-server" -q | xargs docker port | grep "8200/tcp"'
            try:
                port_desc = subprocess.check_output(cmd, shell=True).decode('utf8').strip()
            except subprocess.CalledProcessError:
                print("No running apm-server found. Start it, or provide a server url with --server-url")
                sys.exit(1)
            server_url = 'http://' + port_desc.split(' -> ')[1]
        if sourcemap_file:
            sourcemap_file = os.path.expanduser(sourcemap_file)
            if not os.path.exists(sourcemap_file):
                print('{} not found. Try again :)'.format(sourcemap_file))
                sys.exit(1)
        else:
            try:
                sourcemap_file = glob.glob('./node/sourcemaps/*.map')[0]
            except IndexError:
                print(
                    'No source map found in ./node/sourcemaps.\n'
                    'Start the opbeans-node container, it will create one automatically.'
                )
                sys.exit(1)
        if not bundle_path:
            bundle_path = 'http://opbeans-node:3000/static/js/' + os.path.basename(sourcemap_file)
        if not service_version:
            cmd = (
                'docker ps --filter "name=opbeans-node" -q | '
                'xargs docker inspect -f \'{{range .Config.Env}}{{println .}}{{end}}\' | '
                'grep ELASTIC_APM_JS_BASE_SERVICE_VERSION'
            )
            try:
                build_date = subprocess.check_output(cmd, shell=True).decode('utf8').strip()
                service_version = build_date.split("=")[1]
            except subprocess.CalledProcessError:
                print("opbeans-node container not found. Start it or set --service-version")
                sys.exit(1)
        if self.args.secret_token:
            auth_header = '-H "Authorization: Bearer {}" '.format(self.args.secret_token)
        else:
            auth_header = ''
        print("Uploading {} to {}".format(sourcemap_file, server_url))
        cmd = (
            'curl -X POST '
            '-F service_name="{service_name}" '
            '-F service_version="{service_version}" '
            '-F bundle_filepath="{bundle_path}" '
            '-F sourcemap=@{sourcemap_file} '
            '{auth_header}'
            '{server_url}/v1/client-side/sourcemaps'
        ).format(
            service_name=self.args.service_name,
            service_version=service_version,
            bundle_path=bundle_path,
            sourcemap_file=sourcemap_file,
            auth_header=auth_header,
            server_url=server_url,
        )
        subprocess.check_output(cmd, shell=True).decode('utf8').strip()

    @staticmethod
    def versions_handler():
        Container = collections.namedtuple(
            'Container', ('service', 'stack_version', 'created')
        )
        cmd = (
            'docker ps --filter "name=localtesting" -q | xargs docker inspect '
            '-f \'{{ index .Config.Labels "co.elatic.apm.stack-version" }}\\t{{ .Image }}\\t{{ .Name }}\''
        )

        # Check if Docker is running and get running containers
        try:
            labels = subprocess.check_output(cmd, shell=True).decode('utf8').strip()
            lines = [line.split('\\t') for line in labels.split('\n') if line.split('\\t')[0]]
            for line in lines:
                line[1] = subprocess.check_output(
                    ['docker', 'inspect', '-f', '{{ .Created }}', line[1]]
                ).decode('utf8').strip()
            running_versions = {c.service: c for c in (Container(
                line[2].split('_')[-1],
                line[0],
                datetime.datetime.strptime(line[1].split('.')[0], "%Y-%m-%dT%H:%M:%S")
            ) for line in lines)}
        except subprocess.CalledProcessError:
            # If not, exit immediately
            print('Make sure Docker is running before running this script.')
            sys.exit(1)

        # Check for empty result
        if not running_versions:
            print('No containers are running.')
            print('Make sure the stack is running before checking versions.')
            sys.exit(1)

        # Run all version checks
        print('Getting current version numbers for services...')

        def run_container_command(name, cmd):
            # Get id from docker-compose
            container_id = subprocess.check_output('docker-compose ps -q {}'.format(name),
                                                   shell=True).decode('utf8').strip()

            # Prepare exec command
            command = 'docker exec -it {0} {1}'.format(container_id, cmd)

            # Run command
            try:
                output = subprocess.check_output(
                    command, stderr=open(os.devnull, 'w'), shell=True).decode('utf8').strip()
            except subprocess.CalledProcessError:
                # Handle errors
                print('\tContainer "{}" is not running or an error occurred'.format(name))
                return False

            return output

        def print_elasticsearch_version(container):
            print("\nElasticsearch (image built: %s UTC):" % container.created)

            version = run_container_command(
                'elasticsearch', './bin/elasticsearch --version'
            )

            if version:
                print("\t{0}".format(version))

        def print_apmserver_version(container):
            print("\nAPM Server (image built: %s UTC):" % container.created)

            version = run_container_command('apm-server', 'apm-server version')

            if version:
                print("\t{0}".format(version))

        def print_kibana_version(container):
            print("\nKibana (image built: %s UTC):" % container.created)

            package_json = run_container_command('kibana', 'cat package.json')

            if package_json:

                # Try to parse package.json
                try:
                    data = json.loads(package_json)
                except ValueError as e:
                    print('ERROR: Could not parse Kibana\'s package.json file.')
                    return e

                print("\tVersion: {}".format(data['version']))
                print("\tBranch: {}".format(data['branch']))
                print("\tBuild SHA: {}".format(data['build']['sha']))
                print("\tBuild number: {}".format(data['build']['number']))

        def print_opbeansnode_version(_):
            print("\nAgent version (in opbeans-node):")

            version = run_container_command(
                'opbeans-node', 'npm list | grep elastic-apm-node'
            )

            if version:
                version = version.replace('+-- elastic-apm-node@', '')
                print("\t{0}".format(version))

        def print_opbeanspython_version(_):
            print("\nAgent version (in opbeans-python):")

            version = run_container_command(
                'opbeans-python', 'pip freeze | grep elastic-apm'
            )

            if version:
                version = version.replace('elastic-apm==', '')
                print("\t{0}".format(version))

        def print_opbeansruby_version(_):
            print("\nAgent version (in opbeans-ruby):")

            version = run_container_command(
                'opbeans-ruby', 'gem list | grep elastic-apm'
            )

            if version:
                version = version.replace('elastic-apm (*+)', '\1')
                print("\t{0}".format(version))

        dispatch = {
            'apm-server': print_apmserver_version,
            'elasticsearch': print_elasticsearch_version,
            'kibana': print_kibana_version,
            'opbeans-node': print_opbeansnode_version,
            'opbeans-python': print_opbeanspython_version,
            'opbeans-ruby': print_opbeansruby_version,
        }
        for service_name, container in running_versions.items():
            print_version = dispatch.get(service_name)
            if not print_version:
                print("unknown version for", service_name)
                continue
            print_version(container)


def main():
    # Enable logging
    logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s')
    setup = LocalSetup(sys.argv[1:])
    setup()


if __name__ == '__main__':
    main()
