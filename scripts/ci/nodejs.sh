#!/bin/bash -e

srcdir=`dirname $0`
test -z "$srcdir" && srcdir=.
. ${srcdir}/common.sh

DEFAULT_COMPOSE_ARGS="master --no-apm-server-dashboards --no-kibana --with-agent-nodejs-express --force-build --build-parallel"
export COMPOSE_ARGS=${COMPOSE_ARGS:-${DEFAULT_COMPOSE_ARGS}}
runTests env-agent-nodejs docker-test-agent-nodejs
