from __future__ import division  # Use floating point for math calculations

import fcntl

import requests
from flask import Blueprint, render_template, session, current_app
from flask_apscheduler import APScheduler

from CTFd.api import CTFd_API_v1
from CTFd.plugins import (
    register_plugin_assets_directory,
    register_admin_plugin_menu_bar,
)
from CTFd.plugins.challenges import CHALLENGE_CLASSES
from CTFd.utils.security.csrf import generate_nonce
from .api import *
from .challenge_type import DynamicValueDockerChallenge
from .utils.control import ControlUtil
from .utils.db import DBContainer, DBConfig
from .utils.exceptions import WhaleError
from .utils.redis import RedisUtils
from .utils.setup import setup_default_configs


def load(app):
    # upgrade()
    plugin_name = __name__.split('.')[-1]
    app.db.create_all()
    if not DBConfig.get_config("setup"):
        setup_default_configs()
    CHALLENGE_CLASSES["dynamic_docker"] = DynamicValueDockerChallenge
    register_plugin_assets_directory(
        app, base_path="/plugins/" + plugin_name + "/assets/"
    )

    page_blueprint = Blueprint(
        "ctfd-whale",
        __name__,
        template_folder="templates",
        static_folder="assets",
        url_prefix="/plugins/ctfd-whale"
    )
    register_admin_plugin_menu_bar(
        'Whale', '/plugins/ctfd-whale/admin/settings'
    )
    CTFd_API_v1.add_namespace(admin_namespace, path="/plugins/ctfd-whale/admin")
    CTFd_API_v1.add_namespace(user_namespace, path="/plugins/ctfd-whale")

    @page_blueprint.route('/admin/settings', methods=['GET', 'POST'])
    @admins_only
    def admin_list_configs():
        if request.method == 'POST':
            data = request.form.to_dict()
            data.pop('nonce')
            DBConfig.set_all_configs(data)
            RedisUtils(app=current_app).init_redis_port_sets()
        session["nonce"] = generate_nonce()
        configs = DBConfig.get_all_configs()
        return render_template('config.html', configs=configs)

    @page_blueprint.route("/admin/containers")
    @admins_only
    def admin_list_containers():
        result = AdminContainers.get()
        return render_template("containers.html",
                               plugin_name=plugin_name,
                               containers=result['data']['containers'],
                               pages=result['data']['pages'],
                               curr_page=abs(request.args.get("page", 1, type=int)),
                               curr_page_start=result['data']['page_start'])

    def auto_clean_container():
        with app.app_context():
            results = DBContainer.get_all_expired_container()
            for r in results:
                ControlUtil.try_remove_container(r.user_id)

            configs = DBConfig.get_all_configs()
            containers = DBContainer.get_all_alive_container()

            config = ''.join([c.frp_config for c in containers])

            try:
                # you can authorize a connection by setting
                # frp_url = http://user:pass@ip:port
                frp_addr = configs.get("frp_api_url")
                if not frp_addr:
                    frp_addr = f'http://{configs.get("frp_api_ip", "frpc")}:{configs.get("frp_api_port", "7400")}'
                    # backward compatibility
                common = configs.get("frp_config_template")
                if '[common]' in common:
                    output = common + config
                else:
                    remote = requests.get(f'{frp_addr.lstrip("/")}/api/config')
                    assert remote.status_code == 200
                    configs["frp_config_template"] = remote.text
                    output = remote.text + config
                assert requests.put(
                    f'{frp_addr.lstrip("/")}/api/config', output, timeout=5
                ).status_code == 200
                assert requests.get(
                    f'{frp_addr.lstrip("/")}/api/reload', timeout=5
                ).status_code == 200
            except (requests.RequestException, AssertionError):
                raise WhaleError(
                    'frpc request failed\n' +
                    'please check the frp related configs'
                )

    app.register_blueprint(page_blueprint)

    try:
        lock_file = open("/tmp/ctfd_whale.lock", "w")
        lock_fd = lock_file.fileno()
        fcntl.lockf(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        scheduler = APScheduler()
        scheduler.init_app(app)
        scheduler.start()
        scheduler.add_job(
            id='whale-auto-clean', func=auto_clean_container,
            trigger="interval", seconds=10
        )

        redis_util = RedisUtils(app=app)
        redis_util.init_redis_port_sets()

        print("[CTFd Whale]Started successfully")
    except IOError:
        pass
