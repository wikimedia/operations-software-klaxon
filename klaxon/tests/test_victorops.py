import datetime
import unittest

import requests.exceptions
import responses

import klaxon
from klaxon.victorops import VictorOps, VictorOpsError, Incident


API_ID = 'api_magic_id_1234'
API_KEY = 'secret_key_a1b2c3'
ADMIN_EMAIL = 'klaxon@administrator.org'
CREATE_URL = 'https://test.victorops/abcdef012345/createurl'
API_BASE_URL = 'https://test.victorops/api-public/'


class TestVictorOps(unittest.TestCase):
    v = VictorOps(api_id=API_ID, api_key=API_KEY, create_incident_url=CREATE_URL,
                  admin_email=ADMIN_EMAIL, api_base_url=API_BASE_URL)

    @responses.activate
    def test_send_page(self):
        expected_payload = {
            'message_type': 'CRITICAL',
            'entity_display_name': 'Klaxonbot',
            'state_message': 'You missed a page from victorops',
        }
        responses.add(responses.POST, CREATE_URL, json={'result': 'success', 'entity_id': 'asdf'},
                      match=[responses.json_params_matcher(expected_payload)])

        self.v.send_page(summary="Klaxonbot", description="You missed a page from victorops")

        self.assertEqual(1, len(responses.calls))
        self.assertIn('klaxon', responses.calls[0].request.headers['User-Agent'])
        self.assertIn(ADMIN_EMAIL, responses.calls[0].request.headers['User-Agent'])
        self.assertIn(klaxon.__repository__, responses.calls[0].request.headers['User-Agent'])

    @responses.activate
    def test_send_page_failure(self):
        responses.add(responses.POST, CREATE_URL, body=requests.exceptions.ConnectTimeout())
        self.assertRaises(requests.exceptions.ConnectTimeout,
                          self.v.send_page,
                          summary="This won't work",
                          description="This error won't stop me because I can't read")
        self.assertEqual(1, len(responses.calls))

    @responses.activate
    def test_send_page_httperror(self):
        responses.add(responses.POST, CREATE_URL, body='no healthy upstream', status=503)
        self.assertRaises(requests.exceptions.HTTPError,
                          self.v.send_page,
                          summary="",
                          description="")
        self.assertEqual(1, len(responses.calls))

    @responses.activate
    def test_send_page_vo_error(self):
        responses.add(responses.POST, CREATE_URL, json={'result': 'failure'})
        self.assertRaises(VictorOpsError,
                          self.v.send_page,
                          summary="",
                          description="")
        self.assertEqual(1, len(responses.calls))

    @responses.activate
    def test_fetch_incidents(self):
        resp_payload = {
            'incidents': [
                {
                    'service': 'PerplexityTooHigh_60m',
                    'currentPhase': 'ACKED',
                    'startTime': '2020-12-22T19:02:09.62204Z',
                    'pagedTeams': [],
                },
                {
                    'service': 'this is a summary',
                    'currentPhase': 'UNACKED',
                    'startTime': '2020-12-22T20:02:09.62204Z',
                    'pagedTeams': ['team-xyzzy'],
                },
            ]
        }
        expected = [
            Incident(summary='PerplexityTooHigh_60m',
                     acked=True,
                     time=datetime.datetime(2020, 12, 22, 19, 2, 9, 622040,
                                            tzinfo=datetime.timezone.utc),
                     teams=set([])),
            Incident(summary='this is a summary',
                     acked=False,
                     time=datetime.datetime(2020, 12, 22, 20, 2, 9, 622040,
                                            tzinfo=datetime.timezone.utc),
                     teams=set(['team-xyzzy'])),
        ]
        responses.add(responses.GET, API_BASE_URL + 'v1/incidents',
                      json=resp_payload)

        reply = list(self.v.fetch_incidents())

        self.assertEqual(reply, expected)

        self.assertEqual(1, len(responses.calls))
        self.assertIn('klaxon', responses.calls[0].request.headers['User-Agent'])
        self.assertIn(ADMIN_EMAIL, responses.calls[0].request.headers['User-Agent'])
        self.assertIn(klaxon.__repository__, responses.calls[0].request.headers['User-Agent'])
        self.assertEqual(API_ID, responses.calls[0].request.headers['X-VO-Api-Id'])
        self.assertEqual(API_KEY, responses.calls[0].request.headers['X-VO-Api-Key'])

    @responses.activate
    def test_fetch_incidents_httperror(self):
        responses.add(responses.GET, API_BASE_URL + 'v1/incidents',
                      body='no healthy upstream', status=503)
        with self.assertRaises(requests.exceptions.HTTPError):
            list(self.v.fetch_incidents())
        self.assertEqual(1, len(responses.calls))
