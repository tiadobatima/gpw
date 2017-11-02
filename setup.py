#!/usr/bin/env python3

from setuptools import find_packages
from setuptools import setup


def get_version():
    with open("VERSION") as f:
        return f.readline().rstrip()


def get_install_requirements():
    with open("requirements/pip-install.txt") as f:
        return f.readlines()


def get_test_requirements():
    with open("requirements/pip-test.txt") as f:
        return f.readlines()


config = {
    "name": "gpw",
    "version": get_version(),
    "description": "Multi cloud provider infrastructure-as-code wrapper",
    "author": "Gustavo Baratto",
    "author_email": "gbaratto@gmail.com",
    "url": "https://github.com/tiadobatima/gpw",
    "packages": find_packages("src"),
    "package_dir": {'': 'src'},
    "entry_points": {
        "console_scripts": ["gpw=gpw.cli:main"]
    },
    "setup_requires": ["pytest-runner"],
    "install_requires": install_requires,
    "tests_require": tests_require
}


setup(**config)
