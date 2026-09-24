"""Report generator. The command is built by concatenation."""

import subprocess


def generate(report_name):
    subprocess.run("makereport " + report_name, shell=True)
    return report_name
