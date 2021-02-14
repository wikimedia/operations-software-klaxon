"""A friendly shim over the VictorOps (aka Splunk On-Call) API."""

import dateutil.parser
import datetime
import logging
from dataclasses import dataclass
from typing import AbstractSet, Iterable, Set, Union
from urllib.parse import urljoin

import requests

import klaxon

logger = logging.getLogger(__name__)


@dataclass
class Incident:
    summary: str
    acked: bool
    time: datetime.datetime
    teams: Set[str]


class VictorOpsError(Exception):
    pass


class VictorOps:
    # TODO: perhaps these shouldn't be kw-only args
    def __init__(self, *, api_id: str, api_key: str,
                 create_incident_url: str,
                 team_ids: Union[None, str, AbstractSet[str]] = None,
                 admin_email: str,
                 api_base_url: str = 'https://api.victorops.com/api-public/v1/',
                 repository: str = klaxon.__repository__):
        """Creates a VictorOps API wrapper.

        Parameters
        ----------
            api_id, api_key : str
                The API instance identifier and secret key to be used when fetching incidents.
            create_incident_url : str
                The URL of the REST integration endpoint to be used to create new incidents.
                See https://help.victorops.com/knowledge-base/rest-endpoint-integration-guide/
            team_ids : AbstractSet[str]
                A set of team identifiers to filter open incidents.  If empty, no filter.
            admin_email : str
                An email address for the administrator of this Klaxon interface, used in our
                outgoing User-Agent.
            api_base_url : str
                The root URL of the VictorOps API.  Used for fetching incidents, but not for
                creating new ones -- per the instructions at
                https://portal.victorops.com/public/api-docs.html#!/Incidents/post_api_public_v1_incidents
        """

        self._create_incident_url = create_incident_url
        self._api_base_url = api_base_url

        self.team_ids = None
        if isinstance(team_ids, str) and team_ids:
            self.team_ids = set([team_ids])
        elif team_ids:
            self.team_ids = set(team_ids)

        self._session = requests.Session()
        # A sample of our outgoing User-Agent:
        # klaxon/0.1.0 (https://gerrit.wikimedia.org/r/plugins/gitiles/operations/software/klaxon)
        # instance administered by root@wikimedia.org; python-requests/2.25.0
        self._session.headers['User-Agent'] = (
            f"klaxon/{klaxon.__version__} ({repository}) "
            f"instance administered by {admin_email}; {self._session.headers['User-Agent']}")
        # It isn't a problem to also include these headers when POSTing to the create_incident_url,
        # even though it doesn't actually need or use them.
        self._session.headers['X-VO-Api-Id'] = api_id
        self._session.headers['X-VO-Api-Key'] = api_key

    def send_page(self, summary: str, description: str) -> None:
        """Creates a new paging incident in VictorOps.

        Parameters
        ----------
            summary : str
                A one-line terse title.  Appears in push notifications.
            description : str
                Longer, free-form text.

        Raises:
        -------
        requests.HTTPError
            if the HTTP request failed
        VictorOpsError
            if the HTTP request succeded, but the VictorOps API failed
        """
        payload = {
            'message_type': 'CRITICAL',
            'entity_display_name': summary,
            'state_message': description,
        }
        logging.info("Sending a page: %s", payload)
        resp = self._session.post(self._create_incident_url, json=payload)
        resp.raise_for_status()
        j = resp.json()
        if j['result'] != 'success':
            raise VictorOpsError(j.get('message', ''))

    def fetch_incidents(self) -> Iterable[Incident]:
        """Fetches and yields the current incidents.

        Raises:
        -------
        requests.HTTPError
            if the HTTP request failed
        """
        resp = self._session.get(urljoin(self._api_base_url, 'incidents'))
        resp.raise_for_status()
        j = resp.json()
        for i in j['incidents']:
            if self.team_ids and not set(i['pagedTeams']) & self.team_ids:
                continue
            summary = (i.get('service', None) or i.get('entityDisplayName', None)
                       or i.get('monitorName', None) or 'unknown alert')
            yield Incident(summary=summary,
                           acked=i['currentPhase'] != 'UNACKED',
                           time=dateutil.parser.isoparse(i['startTime']),
                           teams=set(i['pagedTeams']))
