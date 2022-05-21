"""A webapp to allow trusted users to manually page SRE."""

__author__ = "Chris Danis"
__version__ = "0.1.0"
__license__ = "GNU AGPL v3.0"
__repository__ = "https://gerrit.wikimedia.org/r/plugins/gitiles/operations/software/klaxon"
__copyright__ = """
Copyright © 2020 Chris Danis & the Wikimedia Foundation

This program is free software: you can redistribute it and/or modify it under the terms
of the GNU Affero General Public License as published by the Free Software Foundation,
either version 3 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
See the GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License along with
this program.  If not, see <https://www.gnu.org/licenses/>.
"""

import datetime
import logging
import operator
import os
import threading

import cachetools
import werkzeug.exceptions
from flask import Flask, flash, redirect, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix

from klaxon.victorops import VictorOps
from wmflib.irc import SALSocketHandler

CONFIG_DEFAULTS = {
    'KLAXON_REPOSITORY': __repository__,
    'KLAXON_INCIDENT_LIST_CACHE_TTL_SECONDS': '10',
    'KLAXON_INCIDENT_LIST_RECENCY_MINUTES': '60',
    'KLAXON_CAS_AUTH_HEADER': 'CAS-User',
    'KLAXON_CAS_EMAIL_HEADER': 'X-CAS-Mail',
    'KLAXON_VO_API_ID': None,
    'KLAXON_VO_API_KEY': None,
    'KLAXON_VO_CREATE_INCIDENT_URL': None,
    'KLAXON_SECRET_KEY': None,
    'KLAXON_ADMIN_CONTACT_EMAIL': None,
    'KLAXON_TEAM_IDS_FILTER': None,     # A comma-separated list of team IDs, or unset.
    'KLAXON_TCPIRCBOT_HOST': None,
    'KLAXON_TCPIRCBOT_PORT': None,
}


def create_app():
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app)
    if app.config['ENV'] == 'development':
        app.config['TEMPLATES_AUTO_RELOAD'] = True

    for key, default in CONFIG_DEFAULTS.items():
        app.config[key] = os.environ.get(key, default=default)
    # Needed for Flask flash() support.
    app.secret_key = app.config['KLAXON_SECRET_KEY']

    # VictorOps aka Splunk On-Call has rate limits on their API.
    # So, a Klaxon instance does some local caching of API calls, reusing the response for the
    # list of current incidents for a brief interval.
    #
    # This technique only works with the 'gthread' Gunicorn executor model, or similar --
    # your workers need to share an address space.  (Most of the gevent executors should
    # also work.)
    #
    # for example: gunicorn --worker-class gthread --workers 1 --threads 8 'klaxon:create_app()'

    # Max. 1 item in the cache; TTL duration as configured.
    api_cache = cachetools.TTLCache(1, float(app.config['KLAXON_INCIDENT_LIST_CACHE_TTL_SECONDS']))
    api_lock = threading.RLock()

    team_ids = app.config['KLAXON_TEAM_IDS_FILTER']
    if team_ids:
        team_ids = set(team_ids.split(','))
    else:
        team_ids = set()

    vo = VictorOps(api_id=app.config['KLAXON_VO_API_ID'], api_key=app.config['KLAXON_VO_API_KEY'],
                   create_incident_url=app.config['KLAXON_VO_CREATE_INCIDENT_URL'],
                   repository=app.config['KLAXON_REPOSITORY'],
                   admin_email=app.config['KLAXON_ADMIN_CONTACT_EMAIL'],
                   team_ids=team_ids)

    irc_logger = logging.getLogger('klaxon_irc_announce')
    if app.config['KLAXON_TCPIRCBOT_HOST'] and app.config['KLAXON_TCPIRCBOT_PORT']:
        irc_logger.addHandler(SALSocketHandler(app.config['KLAXON_TCPIRCBOT_HOST'],
                                               int(app.config['KLAXON_TCPIRCBOT_PORT']),
                                               'klaxon'))
        irc_logger.setLevel(logging.INFO)

    @cachetools.cached(api_cache, lock=api_lock)
    def fetch_victorops():
        """Return the most recent incidents in reverse chronological order.  Memoized."""
        max_delta = datetime.timedelta(
            minutes=float(app.config['KLAXON_INCIDENT_LIST_RECENCY_MINUTES']))
        now = datetime.datetime.now(datetime.timezone.utc)
        rv = [i for i in vo.fetch_incidents() if now - i.time < max_delta]
        rv.sort(key=operator.attrgetter('time'))
        rv.reverse()
        return rv

    def get_username():
        """From request context, returns the logged-in user or 'unknown' (for local testing)"""
        header = app.config['KLAXON_CAS_AUTH_HEADER']
        if app.config['ENV'] == 'production' and header not in request.headers:
            raise werkzeug.exceptions.Forbidden
        return request.headers.get(header, default='unknown')

    def get_cas_user_email():
        """From request context, returns the logged-in user's email address, if available."""
        header = app.config['KLAXON_CAS_EMAIL_HEADER']
        return request.headers.get(header, default=None)

    def get_user_identity():
        """From request context, returns the logged-in username + email address, if available."""
        email = get_cas_user_email()
        if email:
            return f"{get_username()} ({email})"
        else:
            return get_username()

    @app.route('/')
    def root():
        return render_template('index.html')

    @app.route('/recent_incidents')
    def recent_incidents():
        """Returns an HTML fragment called by JS to fill in the body of a div of recent alerts."""
        incidents = fetch_victorops()
        return render_template('incident_list.html',
                               incidents=incidents)

    @app.route('/protected/page_form')
    def page_form():
        return render_template('page_form.html', identity=get_user_identity(),
                               email=get_cas_user_email())

    @app.route('/protected/submit_page', methods=['POST'])
    def submit_page():
        form = request.form
        # TODO: validate that required fields in the form were included.
        summary = form['summary']
        headline = f"Manual #page by {get_user_identity()}: {summary}"

        irc_logger.info(headline)
        vo.send_page(summary=headline,
                     description=form['description'])

        with api_lock:
            api_cache.clear()
        # We try to prevent ourselves from caching stale data, but the VictorOps API is only
        # eventual consistent, so we present this message to the user anyway.
        flash('Your page was sent. It may not immediately appear in recent alerts, '
              'but it was sent.')
        return redirect('/')

    # Two flavors of the debug handler, so we can inspect both not-logged-in and logged-in state.
    @app.route('/_debug')
    @app.route('/protected/_debug')
    def debug():
        return render_template('debug.html', vars=request.environ)

    return app
