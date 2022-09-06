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
    id: str  # the API calls this 'incidentNumber' but it is actually a string.
    summary: str
    acked: bool  # technically this is acked_or_resolved, but, ¯\_(ツ)_/¯
    time: datetime.datetime
    teams: Set[str]  # a set of team id slugs
    paged_users: Set[str]  # a set of usernames currently being notified (once acked, always empty)


class VictorOpsError(Exception):
    pass


class VictorOps:
    # TODO: perhaps these shouldn't be kw-only args
    def __init__(self, *, api_id: str, api_key: str,
                 create_incident_url: str,
                 team_ids: Union[None, str, AbstractSet[str]] = None,
                 esc_policy_ids: Union[None, str, AbstractSet[str]] = None,
                 admin_email: str,
                 api_base_url: str = 'https://api.victorops.com/',
                 repository: str = klaxon.__repository__):
        """Creates a VictorOps API wrapper.

        Parameters
        ----------
            api_id, api_key : str
                The API instance identifier and secret key to be used when fetching incidents.
            create_incident_url : str
                The URL of the REST integration endpoint to be used to create new incidents.
                See https://help.victorops.com/knowledge-base/rest-endpoint-integration-guide/
            team_ids : Union[None, str, AbstractSet[str]]
                A single team ID or a set of team IDs to filter open incidents & oncall rotations.
                If empty, no filter.
            esc_policy_ids : Union[None, str, AbstractSet[str]]
                A single escalation policy ID or a set of them to filter oncallers.
                If empty, no filter.
            admin_email : str
                An email address for the administrator of this Klaxon interface, used in our
                outgoing User-Agent.
            api_base_url : str
                The root URL of the VictorOps API.  Used for fetching incidents & oncallers,
                but not for creating new ones -- per the instructions at
                https://portal.victorops.com/public/api-docs.html#!/Incidents/post_api_public_v1_incidents
        """

        self._create_incident_url = create_incident_url
        self._api_base_url = api_base_url

        def setOrStringToSet(v):
            if isinstance(v, str) and v:
                return set([v])
            elif v:
                return set(v)
            return None
        self.team_ids = setOrStringToSet(team_ids)
        self.esc_policy_ids = setOrStringToSet(esc_policy_ids)

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

    def _matches_teams(self, ii: Incident) -> bool:
        if not self.team_ids:
            return True
        return bool(ii.teams & self.team_ids)

    def _parse_incident(self, i) -> Incident:
        summary = (i.get('service', None) or i.get('entityDisplayName', None)
                   or i.get('monitorName', None) or 'unknown alert')
        return Incident(summary=summary,
                        acked=i['currentPhase'] != 'UNACKED',
                        id=i['incidentNumber'],
                        paged_users=set(i['pagedUsers']),
                        time=dateutil.parser.isoparse(i['startTime']),
                        teams=set(i['pagedTeams']))

    def fetch_incidents(self) -> Iterable[Incident]:
        """Fetches and yields the current incidents.

        Raises:
        -------
        requests.HTTPError
            if the HTTP request failed
        """
        resp = self._session.get(urljoin(self._api_base_url, 'api-public/v1/incidents'))
        resp.raise_for_status()
        j = resp.json()
        for i in j['incidents']:
            ii = self._parse_incident(i)
            if self._matches_teams(ii):
                yield ii

    def fetch_oncallers(self) -> Iterable[str]:
        resp = self._session.get(urljoin(self._api_base_url, 'api-public/v1/oncall/current'))
        resp.raise_for_status()
        j = resp.json()
        for t in j['teamsOnCall']:
            if self.team_ids and t['team']['slug'] not in self.team_ids:
                continue
            for o in t['oncallNow']:
                pol_id = o['escalationPolicy']['slug']
                if self.esc_policy_ids and pol_id not in self.esc_policy_ids:
                    continue
                for u in o['users']:
                    yield u['onCalluser']['username']

    def reroute_incidents(self, incidents: Iterable[Incident], escalate_to_policy: str,
                          username: str = "escalator_sysuser"):
        if not incidents:
            return
        payload = dict(
            userName=username,
            reroutes=[dict(incidentNumber=i.id,
                           targets=[dict(type="EscalationPolicy", slug=escalate_to_policy)])
                      for i in incidents])
        resp = self._session.post(urljoin(self._api_base_url, "api-public/v1/incidents/reroute"),
                                  json=payload)
        resp.raise_for_status()
        j = resp.json()
        return j

    def escalate_unpaged_incidents(self, escalate_to_policy: str,
                                   username: str = "escalator_sysuser"):
        # If the initial escalation policy (e.g. business hours) pages members immediately,
        # but may sometimes be empty, then that will produce incidents that are in state UNACKED
        # but which also don't have any users listed in paged_users.
        # So let's take those incidents and manually re-route them to the escalate_to_policy
        # (e.g. batphone).
        escalatable = [i for i in self.fetch_incidents() if not i.acked and not i.paged_users]
        return self.reroute_incidents(escalatable, escalate_to_policy, username)

    def check_policy_pages_immediately(self, policy_slug: Iterable[str]):
        '''Check that the given policy has at least one rotation_group with timeout 0.'''
        resp = self._session.get(
            urljoin(self._api_base_url, f"api-public/v1/policies/{policy_slug}"))
        resp.raise_for_status()
        j = resp.json()
        steps = j['steps']
        return any(s for s in steps if s['timeout'] == 0
                   and any(e['executionType'] == 'rotation_group' for e in s['entries']))


if __name__ == '__main__':
    import argparse
    import json
    import logging
    import os
    import sys
    p = argparse.ArgumentParser(
        prog='victorops_cli',
        description=('A simple CLI for VictorOps aka Splunk On-Call.'
                     'API secrets can be read from the same env vars as used by Klaxon.'))
    p.add_argument('--api_id', default=os.environ.get('KLAXON_VO_API_ID'))
    p.add_argument('--api_key', default=os.environ.get('KLAXON_VO_API_KEY'))
    p.add_argument('--admin_email', default=os.environ.get('KLAXON_ADMIN_CONTACT_EMAIL'))
    p.add_argument('--team_ids_filter', default=os.environ.get('KLAXON_TEAM_IDS_FILTER'))
    p.add_argument('-v', '--verbose', action='count', default=0,
                   help='Verbosity (-v, -vv, etc).')
    subparsers = p.add_subparsers(dest='command', metavar="COMMAND")
    escalate_unpaged = subparsers.add_parser(
        'escalate_unpaged', help="Escalate incidents that haven't paged to another rotation")
    escalate_unpaged.add_argument('esc_policy_slug',
                                  help='The escalation policy API slug to reroute to')
    escalate_unpaged.add_argument('-u', '--username', default='escalator_sysuser',
                                  help='The username performing the rerouting')
    check_esc_policy_config = subparsers.add_parser(
        'check_esc_policy_config',
        help='Check that the given list of escalation policies sends a page '
             'to a rotation immediately.')
    check_esc_policy_config.add_argument('esc_policy_slug', nargs='+',
                                         help='One or more escalation policy slugs.')
    args = p.parse_args()
    loglevel = logging.WARNING
    if args.verbose >= 1:
        loglevel = logging.INFO
    if args.verbose >= 2:
        loglevel = logging.DEBUG
    logging.basicConfig(level=loglevel)
    team_ids_filter = None
    if args.team_ids_filter:
        team_ids_filter = args.team_ids_filter.split(',')
    v = VictorOps(api_id=args.api_id, api_key=args.api_key,
                  team_ids=team_ids_filter,
                  create_incident_url=None, admin_email=args.admin_email)

    if args.command == 'escalate_unpaged':
        rv = v.escalate_unpaged_incidents(escalate_to_policy=args.esc_policy_slug,
                                          username=args.username)
        if rv:
            print(json.dumps(rv, indent=4))
    if args.command == 'check_esc_policy_config':
        rv = 0
        for policy in args.esc_policy_slug:
            if not v.check_policy_pages_immediately(policy):
                print(f"ERROR: Policy {policy} does not immediately page any rotation_group!")
                rv = 2  # nagios CRITICAL
        sys.exit(rv)
