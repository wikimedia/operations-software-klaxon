#!/usr/bin/env python3

import setuptools

setuptools.setup(
    name="klaxon",
    version="0.1.0",
    author="Chris Danis",
    author_email="cdanis@wikimedia.org",
    description="",
    url="https://gerrit.wikimedia.org/r/plugins/gitiles/operations/software/klaxon",
    packages=setuptools.find_packages(),
    classifiers=[
        "Programming Language :: Python :: 3",
        "Development Status :: 3 - Alpha",
        "Topic :: Internet :: WWW/HTTP :: WSGI :: Application",
        "Operating System :: OS Independent",
    ],
    python_requires='>=3.7',
    install_requires=[
        "wmflib>=0.0.6",
        # These are the versions packaged in Debian Buster.
        "requests>=2.21.0",
        "flask==1.0.2",
        "itsdangerous==0.24",
        "cachetools==4.2.0",
        "python-dateutil==2.8.1",
    ],
    extras_require={'tests': [
        # json_params_matcher added in this version.
        "responses>=0.11.0",
    ]},
)
