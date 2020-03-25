"""
Flask backend

.. note::
    在 AsyncBasedSession 会话中，若在协程任务函数内调用 asyncio 中的协程函数，需要使用 asyncio_coroutine


.. attention::
    PyWebIO 的会话状态保存在进程内，所以不支持多进程部署的Flask。
        比如使用 ``uWSGI`` 部署Flask，并使用 ``--processes n`` 选项设置了多进程；
        或者使用 ``nginx`` 等反向代理将流量负载到多个 Flask 副本上。

    A note on run Flask with uWSGI：

    If you start uWSGI without threads, the Python GIL will not be enabled,
    so threads generated by your application will never run. `uWSGI doc <https://uwsgi-docs.readthedocs.io/en/latest/WSGIquickstart.html#a-note-on-python-threads>`_
    在Flask backend中，PyWebIO使用单独一个线程来运行事件循环。如果程序中没有使用到asyncio中的协程函数，
    可以在 start_flask_server 参数中设置 ``disable_asyncio=False`` 来关闭对asyncio协程函数的支持。
    如果您需要使用asyncio协程函数，那么需要在在uWSGI中使用 ``--enable-thread`` 选项开启线程支持。

"""
import asyncio
import threading
import time
from functools import partial
from typing import Dict

from flask import Flask, request, jsonify, send_from_directory

from ..session import CoroutineBasedSession, ThreadBasedSession, get_session_implement, AbstractSession, \
    mark_server_started
from ..utils import STATIC_PATH
from ..utils import random_str, LRUDict

# todo: use lock to avoid thread race condition
_webio_sessions: Dict[str, CoroutineBasedSession] = {}  # WebIOSessionID -> WebIOSession()
_webio_expire = LRUDict()  # WebIOSessionID -> last active timestamp

DEFAULT_SESSION_EXPIRE_SECONDS = 60 * 60 * 4  # 超过4个小时会话不活跃则视为会话过期
REMOVE_EXPIRED_SESSIONS_INTERVAL = 120  # 清理过期会话间隔（秒）

_event_loop = None


def _make_response(webio_session: AbstractSession):
    return jsonify(webio_session.get_task_messages())


def _remove_expired_sessions(session_expire_seconds):
    while _webio_expire:
        sid, active_ts = _webio_expire.popitem(last=False)
        if time.time() - active_ts < session_expire_seconds:
            _webio_expire[sid] = active_ts
            _webio_expire.move_to_end(sid, last=False)
            break
        del _webio_sessions[sid]


_last_check_session_expire_ts = 0  # 上次检查session有效期的时间戳


def _remove_webio_session(sid):
    del _webio_sessions[sid]
    del _webio_expire[sid]


def _webio_view(coro_func, session_expire_seconds):
    """
    todo use cookie instead of session
    :param coro_func:
    :param session_expire_seconds:
    :return:
    """
    if request.args.get('test'):  # 测试接口，当会话使用给予http的backend时，返回 ok
        return 'ok'

    global _last_check_session_expire_ts, _event_loop
    if _event_loop:
        asyncio.set_event_loop(_event_loop)

    webio_session_id = None
    set_header = False
    if 'webio-session-id' not in request.headers or not request.headers['webio-session-id']:  # start new WebIOSession
        set_header = True
        webio_session_id = random_str(24)
        if get_session_implement() is CoroutineBasedSession:
            webio_session = CoroutineBasedSession(coro_func)
        else:
            webio_session = ThreadBasedSession(coro_func)
        _webio_sessions[webio_session_id] = webio_session
        _webio_expire[webio_session_id] = time.time()
    elif request.headers['webio-session-id'] not in _webio_sessions:  # WebIOSession deleted
        return jsonify([dict(command='close_session')])
    else:
        webio_session_id = request.headers['webio-session-id']
        webio_session = _webio_sessions[webio_session_id]

    if request.method == 'POST':  # client push event
        webio_session.send_client_event(request.json)

    elif request.method == 'GET':  # client pull messages
        pass

    if time.time() - _last_check_session_expire_ts > REMOVE_EXPIRED_SESSIONS_INTERVAL:
        _remove_expired_sessions(session_expire_seconds)
        _last_check_session_expire_ts = time.time()

    response = _make_response(webio_session)
    if webio_session.closed():
        _remove_webio_session(webio_session_id)
    elif set_header:
        response.headers['webio-session-id'] = webio_session_id
    return response


def webio_view(coro_func, session_expire_seconds):
    """获取Flask view"""
    view_func = partial(_webio_view, coro_func=coro_func, session_expire_seconds=session_expire_seconds)
    view_func.__name__ = 'webio_view'
    return view_func


def _setup_event_loop():
    global _event_loop
    _event_loop = asyncio.new_event_loop()
    _event_loop.set_debug(True)
    asyncio.set_event_loop(_event_loop)
    _event_loop.run_forever()


def start_flask_server(coro_func, port=8080, host='localhost',
                       session_type=None,
                       disable_asyncio=False,
                       session_expire_seconds=DEFAULT_SESSION_EXPIRE_SECONDS,
                       debug=False, **flask_options):
    """
    :param coro_func:
    :param port:
    :param host:
    :param str session_type: Session <pywebio.session.AbstractSession>` 的实现，默认为基于线程的会话实现。
        接受的值为 `pywebio.session.THREAD_BASED` 和 `pywebio.session.COROUTINE_BASED`
    :param disable_asyncio: 禁用 asyncio 函数。仅在当 ``session_type=COROUTINE_BASED`` 时有效。
        在Flask backend中使用asyncio需要单独开启一个线程来运行事件循环，
        若程序中没有使用到asyncio中的异步函数，可以开启此选项来避免不必要的资源浪费
    :param session_expire_seconds:
    :param debug:
    :param flask_options:
    :return:
    """
    mark_server_started(session_type)

    app = Flask(__name__)
    app.route('/io', methods=['GET', 'POST'])(webio_view(coro_func, session_expire_seconds))

    @app.route('/')
    def index_page():
        return send_from_directory(STATIC_PATH, 'index.html')

    @app.route('/<path:static_file>')
    def serve_static_file(static_file):
        return send_from_directory(STATIC_PATH, static_file)

    if not disable_asyncio and get_session_implement() is CoroutineBasedSession:
        threading.Thread(target=_setup_event_loop, daemon=True).start()

    app.run(host=host, port=port, debug=debug, **flask_options)
