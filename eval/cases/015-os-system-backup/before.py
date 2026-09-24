import os


def archive_directory(directory, label):
    """Tar a directory into /var/backups and return the archive path."""
    destination = f"/var/backups/{label}.tar.gz"
    os.system("tar czf " + destination + " " + directory)
    return destination
