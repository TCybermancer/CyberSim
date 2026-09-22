import socket

import agent


def test_oob_binding_does_not_change_application_sockets():
    original = socket.socket.__init__
    session = agent.bound_session('192.168.200.139')
    assert socket.socket.__init__ is original
    for scheme in ('http://', 'https://'):
        assert session.get_adapter(scheme).poolmanager.connection_pool_kw['source_address'] == ('192.168.200.139', 0)
    with socket.socket() as connection:
        assert connection.getsockname()[0] == '0.0.0.0'
    session.close()


def test_no_oob_address_retains_normal_http_adapter():
    session = agent.bound_session(None)
    assert 'source_address' not in session.get_adapter('http://').poolmanager.connection_pool_kw
    session.close()


def test_proxy_control_plane_keeps_oob_binding():
    session = agent.bound_session('192.168.200.139')
    proxy = session.get_adapter('https://').proxy_manager_for('http://127.0.0.1:8888')
    assert proxy.connection_pool_kw['source_address'] == ('192.168.200.139', 0)
    session.close()
