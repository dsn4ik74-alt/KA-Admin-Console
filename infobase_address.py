"""Client connection strings use the cluster port returned by RAC."""


def infobase_address(cluster, database, server=''):
    host = str(cluster.get('host') or server).strip()
    try:
        port = int(cluster.get('port', 0))
    except (ValueError, TypeError):
        return ''
    if not host or not database or not 0 < port <= 65535:
        return ''
    if any(character in host + database for character in '\r\n\x00'):
        return ''
    if ':' in host and not host.startswith('['):
        host = '[' + host + ']'
    endpoint = (host + ':' + str(port)).replace('"', '""')
    database = database.replace('"', '""')
    return f'Srvr="{endpoint}";Ref="{database}";'
