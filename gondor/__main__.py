import argparse
import ConfigParser
import getpass
import os
import re
import subprocess
import sys
import time
import urllib
import urllib2
import zlib

try:
    import simplejson as json
except ImportError:
    import json

from gondor import __version__
from gondor import http, utils
from gondor.api import make_api_call
from gondor.progressbar import ProgressBar


out = utils.out


RE_VALID_USERNAME = re.compile('[\w.@+-]+$')
EMAIL_RE = re.compile(
    r"(^[-!#$%&'*+/=?^_`{}|~0-9A-Z]+(\.[-!#$%&'*+/=?^_`{}|~0-9A-Z]+)*"  # dot-atom
    r'|^"([\001-\010\013\014\016-\037!#-\[\]-\177]|\\[\001-\011\013\014\016-\177])*"' # quoted-string
    r')@(?:[A-Z0-9-]+\.)+[A-Z]{2,6}$', # domain
    re.IGNORECASE)
DEFAULT_ENDPOINT = "https://api.gondor.io"


def config_value(config, section, key, default=None):
    try:
        return config.get(section, key)
    except (ConfigParser.NoOptionError, ConfigParser.NoSectionError):
        return default


def cmd_init(args, config):
    site_key = args.site_key[0]
    if len(site_key) < 11:
        sys.stderr.write("The site key given is too short.\n")
        sys.exit(1)
    
    # ensure os.getcwd() is a Django directory
    files = [
        os.path.join(os.getcwd(), "__init__.py"),
        os.path.join(os.getcwd(), "manage.py")
    ]
    if not all([os.path.exists(f) for f in files]):
        sys.stderr.write("You must run gondor init from a Django project directory.\n")
        sys.exit(1)
    
    gondor_dir = os.path.abspath(os.path.join(os.getcwd(), ".gondor"))
    
    if not os.path.exists(gondor_dir):
        os.mkdir(gondor_dir)
        
        # write out a .gondor/config INI file
        new_config = ConfigParser.RawConfigParser()
        new_config.add_section("gondor")
        new_config.set("gondor", "site_key", site_key)
        new_config.set("gondor", "vcs", "git")
        new_config.add_section("app")
        new_config.set("app", "requirements_file", "requirements/project.txt")
        new_config.set("app", "wsgi_entry_point", "deploy.wsgi")
        new_config.set("app", "migrations", "none")
        with open(os.path.join(gondor_dir, "config"), "wb") as cf:
            new_config.write(cf)


def cmd_create(args, config):
    gondor_dirname = ".gondor"
    try:
        project_root = utils.find_nearest(os.getcwd(), gondor_dirname)
    except OSError:
        sys.stderr.write("Unable to find a .gondor directory.\n")
        sys.exit(1)
    
    label = args.label[0]
    
    kind = args.kind
    if kind is None:
        kind = "dev"
    
    out("Reading configuration... ")
    local_config = ConfigParser.RawConfigParser()
    local_config.read(os.path.join(project_root, gondor_dirname, "config"))
    endpoint = config_value(local_config, "gondor", "endpoint", DEFAULT_ENDPOINT)
    site_key = local_config.get("gondor", "site_key")
    out("[ok]\n")
    
    text = "Creating instance on Gondor... "
    url = "%s/create/" % endpoint
    params = {
        "version": __version__,
        "site_key": site_key,
        "label": label,
        "kind": kind,
        "project_root": os.path.basename(project_root),
    }
    response = make_api_call(config, url, urllib.urlencode(params))
    data = json.loads(response.read())
    if data["status"] == "error":
        message = "error"
    elif data["status"] == "success":
        message = "ok"
    else:
        message = "unknown"
    out("\r%s[%s]   \n" % (text, message))
    if data["status"] == "success":
        out("\nRun: gondor deploy %s HEAD" % label)
        out("\nVisit: %s\n" % data["url"])
    else:
        out("\nError: %s\n" % data["message"])


def cmd_deploy(args, config):
    label = args.label[0]
    commit = args.commit[0]
    
    gondor_dirname = ".gondor"
    try:
        project_root = utils.find_nearest(os.getcwd(), gondor_dirname)
    except OSError:
        sys.stderr.write("Unable to find a .gondor directory.\n")
        sys.exit(1)
    
    tarball = None
    
    try:
        out("Reading configuration... ")
        local_config = ConfigParser.RawConfigParser()
        local_config.read(os.path.join(project_root, gondor_dirname, "config"))
        endpoint = config_value(local_config, "gondor", "endpoint", DEFAULT_ENDPOINT)
        site_key = local_config.get("gondor", "site_key")
        vcs = local_config.get("gondor", "vcs")
        app_config = {
            "requirements_file": config_value(local_config, "app", "requirements_file"),
            "wsgi_entry_point": config_value(local_config, "app", "wsgi_entry_point"),
            "migrations": config_value(local_config, "app", "migrations"),
        }
        out("[ok]\n")
        
        if vcs == "git":
            try:
                repo_root = utils.find_nearest(os.getcwd(), ".git")
            except OSError:
                sys.stderr.write("Unable to find a .git directory.\n")
                sys.exit(1)
            sha = utils.check_output("git rev-parse %s" % commit).strip()
            if commit == "HEAD":
                commit = sha
            tarball = os.path.abspath(os.path.join(repo_root, "%s-%s.tar.gz" % (label, sha)))
            cmd = "(cd %s && git archive --format=tar %s | gzip > %s)" % (repo_root, commit, tarball)
        else:
            raise NotImplementedError()
        
        out("Building tarball from %s... " % commit)
        subprocess.call([cmd], shell=True)
        out("[ok]\n")
        
        pb = ProgressBar(0, 100, 77)
        out("Pushing tarball to Gondor... \n")
        url = "%s/deploy/" % endpoint
        params = {
            "version": __version__,
            "site_key": site_key,
            "label": label,
            "sha": sha,
            "commit": commit,
            "tarball": open(tarball, "rb"),
            "project_root": os.path.relpath(project_root, repo_root),
            "app": json.dumps(app_config),
        }
        handlers = [
            http.MultipartPostHandler,
            http.UploadProgressHandler(pb, ssl=True),
            http.UploadProgressHandler(pb, ssl=False)
        ]
        response = make_api_call(config, url, params, extra_handlers=handlers)
        out("\n")
        data = json.loads(response.read())
        if data["status"] == "error":
            out("\nError: %s\n" % data["message"])
        if data["status"] == "success":
            deployment_id = data["deployment"]
            if "url" in data:
                instance_url = data["url"]
            else:
                instance_url = None
            
            # poll status of the deployment
            out("Deploying... ")
            while True:
                params = {
                    "version": __version__,
                    "site_key": site_key,
                    "instance_label": label,
                    "deployment_id": deployment_id,
                }
                url = "%s/deployment_status/" % endpoint
                try:
                    response = make_api_call(config, url, urllib.urlencode(params))
                except urllib2.URLError:
                    # @@@ add max retries
                    continue
                data = json.loads(response.read())
                if data["status"] == "error":
                    out("[error]\n")
                    out("\nError: %s\n" % data["message"])
                if data["status"] == "success":
                    if data["state"] == "deployed":
                        out("[ok]\n")
                        if instance_url:
                            out("\nVisit: %s\n" % instance_url)
                        break
                    elif data["state"] == "failed":
                        out("[failed]\n")
                        out("\n%s\n" % data["reason"])
                        break
                    elif data["state"] == "locked":
                        out("[locked]\n")
                        out("\nYour deployment failed due to being locked. This means there is another deployment already in progress.\n")
                        break
                    else:
                        time.sleep(1)
    finally:
        if tarball:
            os.unlink(tarball)


def cmd_sqldump(args, config):
    label = args.label[0]
    
    gondor_dirname = ".gondor"
    repo_root = utils.find_nearest(os.getcwd(), gondor_dirname)
    
    local_config = ConfigParser.RawConfigParser()
    local_config.read(os.path.join(repo_root, gondor_dirname, "config"))
    endpoint = config_value(local_config, "gondor", "endpoint", DEFAULT_ENDPOINT)
    site_key = local_config.get("gondor", "site_key")
    
    # request SQL dump and stream the response through uncompression
    
    d = zlib.decompressobj(16+zlib.MAX_WBITS)
    url = "%s/sqldump/" % endpoint
    params = {
        "version": __version__,
        "site_key": site_key,
        "label": label,
    }
    response = make_api_call(config, url, urllib.urlencode(params))
    cs = 16 * 1024
    while True:
        chunk = response.read(cs)
        if not chunk:
            break
        out(d.decompress(chunk))


def cmd_addon(args, config):
    
    addon_label = args.addon_label[0]
    instance_label = args.instance_label[0]
    
    gondor_dirname = ".gondor"
    try:
        project_root = utils.find_nearest(os.getcwd(), gondor_dirname)
    except OSError:
        sys.stderr.write("Unable to find a .gondor directory.\n")
        sys.exit(1)
    
    out("Reading configuration... ")
    local_config = ConfigParser.RawConfigParser()
    local_config.read(os.path.join(project_root, gondor_dirname, "config"))
    endpoint = config_value(local_config, "gondor", "endpoint", DEFAULT_ENDPOINT)
    site_key = local_config.get("gondor", "site_key")
    out("[ok]\n")
    
    text = "Adding addon to your instance... "
    out(text)
    url = "%s/addon/" % endpoint
    params = {
        "version": __version__,
        "site_key": site_key,
        "addon_label": addon_label,
        "instance_label": instance_label,
    }
    response = make_api_call(config, url, urllib.urlencode(params))
    data = json.loads(response.read())
    if data["status"] == "error":
        message = "error"
    elif data["status"] == "success":
        message = "ok"
    else:
        message = "unknown"
    out("\r%s[%s]   \n" % (text, message))
    if data["status"] == "error":
        out("\nError: %s\n" % data["message"])


def cmd_run(args, config):
    
    instance_label = args.instance_label[0]
    command = args.command_[0]
    params = {}
    
    gondor_dirname = ".gondor"
    try:
        project_root = utils.find_nearest(os.getcwd(), gondor_dirname)
    except OSError:
        sys.stderr.write("Unable to find a .gondor directory.\n")
        sys.exit(1)
    
    out("Reading configuration... ")
    local_config = ConfigParser.RawConfigParser()
    local_config.read(os.path.join(project_root, gondor_dirname, "config"))
    endpoint = config_value(local_config, "gondor", "endpoint", DEFAULT_ENDPOINT)
    site_key = local_config.get("gondor", "site_key")
    app_config = {
        "requirements_file": config_value(local_config, "app", "requirements_file"),
        "wsgi_entry_point": config_value(local_config, "app", "wsgi_entry_point"),
        "migrations": config_value(local_config, "app", "migrations"),
    }
    out("[ok]\n")
    
    if command == "createsuperuser":
        try:
            # Get a username
            while 1:
                username = raw_input("Username: ")
                if not RE_VALID_USERNAME.match(username):
                    sys.stderr.write("Error: That username is invalid. Use only letters, digits and underscores.\n")
                    username = None
                    continue
                break
            
            # Get an e-mail
            while 1:
                email = raw_input("E-mail address: ")
                if not EMAIL_RE.search(email):
                    sys.stderr.write("Error: That e-mail address is invalid.\n")
                    email = None
                else:
                    break
            
            # Get a password
            while 1:
                password = getpass.getpass()
                password2 = getpass.getpass("Password (again): ")
                if password != password2:
                    sys.stderr.write("Error: Your passwords didn't match.\n")
                    password = None
                    continue
                if password.strip() == "":
                    sys.stderr.write("Error: Blank passwords aren't allowed.\n")
                    password = None
                    continue
                break
        except KeyboardInterrupt:
            sys.stderr.write("\nOperation cancelled.\n")
            sys.exit(1)
        
        params = {
            "username": username,
            "email": email,
            "password": password,
        }
    
    out("Executing... ")
    url = "%s/run/" % endpoint
    params = {
        "version": __version__,
        "site_key": site_key,
        "instance_label": instance_label,
        "command": command,
        "params": json.dumps(params),
        "app": json.dumps(app_config),
    }
    response = make_api_call(config, url, urllib.urlencode(params))
    data = json.loads(response.read())
    
    if data["status"] == "error":
        out("[error]\n")
        out("\nError: %s\n" % data["message"])
    if data["status"] == "success":
        task_id = data["task"]
        while True:
            params = {
                "version": __version__,
                "site_key": site_key,
                "instance_label": instance_label,
                "deployment_id": task_id,
            }
            url = "%s/task_status/" % endpoint
            response = make_api_call(config, url, urllib.urlencode(params))
            data = json.loads(response.read())
            if data["status"] == "error":
                out("[error]\n")
                out("\nError: %s\n" % data["message"])
            if data["status"] == "success":
                if data["state"] == "executed":
                    out("[ok]\n")
                    break
                elif data["state"] == "failed":
                    out("[failed]\n")
                    out("\n%s\n" % data["reason"])
                    break
                elif data["state"] == "locked":
                    out("[locked]\n")
                    out("\nYour execution failed due to being locked. This means there is another execution already in progress.\n")
                    break
                else:
                    time.sleep(2)


def cmd_delete(args, config):
    
    instance_label = args.label[0]
    
    gondor_dirname = ".gondor"
    try:
        project_root = utils.find_nearest(os.getcwd(), gondor_dirname)
    except OSError:
        sys.stderr.write("Unable to find a .gondor directory.\n")
        sys.exit(1)
    
    out("Reading configuration... ")
    local_config = ConfigParser.RawConfigParser()
    local_config.read(os.path.join(project_root, gondor_dirname, "config"))
    endpoint = config_value(local_config, "gondor", "endpoint", DEFAULT_ENDPOINT)
    site_key = local_config.get("gondor", "site_key")
    out("[ok]\n")
    
    text = "ARE YOU SURE YOU WANT TO DELETE THIS INSTANCE? [Y/N] "
    out(text)
    user_input = raw_input()
    if user_input != "Y":
        out("Exiting without deleting the instance.\n")
        sys.exit(0)
    text = "Deleting... "
    
    url = "%s/delete/" % endpoint
    params = {
        "version": __version__,
        "site_key": site_key,
        "instance_label": instance_label,
    }
    response = make_api_call(config, url, urllib.urlencode(params))
    data = json.loads(response.read())
    if data["status"] == "error":
        message = "error"
    elif data["status"] == "success":
        message = "ok"
    else:
        message = "unknown"
    out("\r%s[%s]   \n" % (text, message))
    if data["status"] == "error":
        out("\nError: %s\n" % data["message"])


def cmd_list(args, config):
    
    gondor_dirname = ".gondor"
    try:
        project_root = utils.find_nearest(os.getcwd(), gondor_dirname)
    except OSError:
        sys.stderr.write("Unable to find a .gondor directory.\n")
        sys.exit(1)
    
    out("Reading configuration... ")
    local_config = ConfigParser.RawConfigParser()
    local_config.read(os.path.join(project_root, gondor_dirname, "config"))
    endpoint = config_value(local_config, "gondor", "endpoint", DEFAULT_ENDPOINT)
    site_key = local_config.get("gondor", "site_key")
    out("[ok]\n")
    
    url = "%s/list/" % endpoint
    params = {
        "version": __version__,
        "site_key": site_key,
    }
    response = make_api_call(config, url, urllib.urlencode(params))
    data = json.loads(response.read())
    
    if data["status"] == "success":
        out("\n")
        for instance in sorted(data["instances"], key=lambda v: v["label"]):
            out("%s [%s] %s\n" % (
                instance["label"],
                instance["kind"],
                instance["last_deployment"]["sha"][:8]
            ))
    else:
        out("\nError: %s\n" % data["message"])


def main():
    parser = argparse.ArgumentParser(prog="gondor")
    parser.add_argument("--version", action="version", version="%%(prog)s %s" % __version__)
    
    command_parsers = parser.add_subparsers(dest="command")
    
    # cmd: init
    parser_init = command_parsers.add_parser("init")
    parser_init.add_argument("site_key", nargs=1)
    
    # cmd: create
    parser_create = command_parsers.add_parser("create")
    parser_create.add_argument("--kind")
    parser_create.add_argument("label", nargs=1)
    
    # cmd: deploy
    parser_deploy = command_parsers.add_parser("deploy")
    parser_deploy.add_argument("label", nargs=1)
    parser_deploy.add_argument("commit", nargs=1)
    
    # cmd: sqldump
    parser_sqldump = command_parsers.add_parser("sqldump")
    parser_sqldump.add_argument("label", nargs=1)
    
    # cmd: addon
    parser_addon = command_parsers.add_parser("addon")
    parser_addon.add_argument("addon_label", nargs=1)
    parser_addon.add_argument("instance_label", nargs=1)
    
    # cmd: run
    parser_run = command_parsers.add_parser("run")
    parser_run.add_argument("instance_label", nargs=1)
    parser_run.add_argument("command_", nargs=1)
    
    # cmd: delete
    parser_delete = command_parsers.add_parser("delete")
    parser_delete.add_argument("label", nargs=1)
    
    # cmd: list
    parser_list = command_parsers.add_parser("list")
    
    args = parser.parse_args()
    
    # config
    
    config = ConfigParser.RawConfigParser()
    config.read(os.path.expanduser("~/.gondor"))
    config = {
        "username": config.get("auth", "username"),
        "password": config.get("auth", "password"),
    }
    
    {
        "init": cmd_init,
        "create": cmd_create,
        "deploy": cmd_deploy,
        "sqldump": cmd_sqldump,
        "addon": cmd_addon,
        "run": cmd_run,
        "delete": cmd_delete,
        "list": cmd_list,
    }[args.command](args, config)
