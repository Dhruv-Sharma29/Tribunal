import os


def extract_member(archive, member_name, destination_dir):
    """Write one archive member into destination_dir and return the path written."""
    target = os.path.join(destination_dir, member_name)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as handle:
        handle.write(archive.read(member_name))
    return target
