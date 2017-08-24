#!/usr/bin/env python3

import io
from setuptools import find_packages
from setuptools import setup


def get_version():
    for i in io.open("env", "r", encoding="utf-8"):
        if "VERSION=" in i:
            return i.split("=")[1].rstrip()


config = {
    "name": "gpw",
    "version": get_version(),
    "description": "Cloud provider infrastructure-as-code wrapper",
    "author": "Gustavo Baratto",
    "author_email": "gus.baratto@sourcedgroup.com",
    "url": "https://bitbucket.org/sourcedgroup/gpw",
    "packages": find_packages(),
    "entry_points": {
        "console_scripts": ["gpw=gpw.cli:main"]
    }
}


setup(**config)
