from tests.endpoint import Endpoint
from tests import utils
import requests


def test_rum(rum):
    elasticsearch = rum.apm_server.elasticsearch
    elasticsearch.clean()
    endpoint = Endpoint(rum.url, "run_integration_test", qu_str="echo=done", text="done")

    r = requests.get(endpoint.url)
    utils.check_request_response(r, endpoint)
    utils.check_elasticsearch_transaction(elasticsearch, 1, {'query': {'term': {'processor.event': 'transaction'}}})
