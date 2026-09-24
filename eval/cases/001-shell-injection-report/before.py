import subprocess


def generate_report(report_name, output_dir):
    """Render a named report into output_dir and return the path written."""
    destination = f"{output_dir}/{report_name}.pdf"
    subprocess.run("makereport " + report_name + " > " + destination, shell=True)
    return destination
