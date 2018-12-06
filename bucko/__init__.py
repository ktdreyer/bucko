import argparse
from pprint import pformat
import tempfile
import json
import os
from .log import log
from bucko import config
from bucko.repo_compose import RepoCompose
from bucko.publisher import Publisher
from bucko.koji_builder import KojiBuilder
from bucko.registry import Registry

__version__ = '1.0.0'

__all__ = ['log']


def parse_ci_message(msg, compose_url):
    """
    Parse CI_MESSAGE JSON data and return a compose URL according to our rules.

    :param         msg: ``dict`` of JSON data from CI_MESSAGE environment var
    :param compose_url: ``str`` COMPOSE_URL environment var (might be a format
                        string that we will interpolate from msg values.)
    :return: ``str`` compose URL.
    """
    log.info('Parsing CI_MESSAGE: %s' % pformat(msg))
    try:
        return msg['compose_url']
    except KeyError:
        log.info('CI_MESSAGE JSON lacks "compose_url" key.')
    # Not a product-build-done message?
    try:
        # Maybe CI_MESSAGE was a dist-git message?
        branch = msg['branch']
    except KeyError:
        log.info('CI_MESSAGE JSON lacks "branch" key.')
        log.info('Falling back to COMPOSE_URL env variable.')
        return compose_url
    # Parse our "branch" JSON key and interpolate values into the
    # COMPOSE_URL environment variable (format string).
    (_, version, distro) = branch.split('-', 2)
    major = int(float(version))
    distro = distro.upper()
    result = compose_url % {'branch': branch,
                            'major': major,
                            'distro': distro}
    log.info('transformed %s format string to %s' % (compose_url, result))
    return result


def compose_url_from_env():
    """
    Parse COMPOSE_URL and CI_MESSAGE environment variables for a URL.

    If we can't find a URL, return None.

    Exact rules:
      1. Search the JSON in CI_MESSAGE first, return compose_url key.
           {"compose_url": "http://example.com/foo"}
      2. If the JSON lacks a compose_url key, search for a "branch" key
         instead. Parse that, and interpolate branch/major into the COMPOSE_URL
         env variable.
           {"branch": "ceph-3.0-rhel-7"}
           COMPOSE_URL=http://example.com/(branch)s/latest-RHCEPH-(major)s
      3. If CI_MESSAGE is not valid JSON, fall back to checking the COMPOSE_URL
         env var. The assumption here is that this is a manual Jenkins job run.
      4. If COMPOSE_URL env var is empty (or undefined), return None.
    """
    compose_url = os.environ.get('COMPOSE_URL', '')
    if compose_url == '':
        compose_url = None
    try:
        msg = json.loads(os.environ.get('CI_MESSAGE', ''))
    except ValueError:
        # No CI_MESSAGE JSON. Falling back to COMPOSE_URL environment var
        return compose_url
    return parse_ci_message(msg, compose_url)


def get_publisher(configp):
    """ Look up the push url and http url from a ConfigParser object. """
    push_url = config.lookup(configp, 'publish', 'push')
    http_url = config.lookup(configp, 'publish', 'http')
    return Publisher(push_url, http_url)


def write_metadata_file(filename, **kwargs):
    """ Write metadata to a JSON file. """
    filename = os.path.join(tempfile.mkdtemp(suffix='.json'), filename)
    with open(filename, 'w') as f:
        json.dump(kwargs, f, sort_keys=True)
    return filename


def write_props_file(**kwargs):
    """ Write data into a .props file for Jenkins to read. """
    if 'WORKSPACE' in os.environ:
        log.info('WORKSPACE detected, writing osbs.props for Jenkins')
        props_path = os.path.join(os.environ['WORKSPACE'], 'osbs.props')
        with open(props_path, 'w') as props:
            for key, value in kwargs.iteritems():
                props.write(key.upper() + '=' + str(value) + "\n")


def get_compose(compose_url, configp):
    """ Construct a RepoCompose object according to our ConfigParser. """
    keys = dict(configp.items('keys'))
    compose = RepoCompose(compose_url, keys)
    section = get_branch(compose) + '-base'  # eg "ceph-2-rhel-7-base"
    bp_url = config.lookup(configp, section, 'url')
    bp_gpgkey = config.lookup(configp, section, 'gpgkey', fatal=False)
    bp_extras = config.lookup(configp, section, 'extras', fatal=False)
    bp_parent_image = config.lookup(configp, section, 'parent_image', fatal=False)
    compose.set_base_product(bp_url, bp_gpgkey, bp_extras, bp_parent_image)
    return compose


def get_branch(compose):
    """ Return a dist-git branch name for this compose. """
    name = compose.info.release.short.lower()
    if name == 'rhceph':
        name = 'ceph'
    version = compose.info.release.version
    if name == 'ceph' and version.startswith('2'):
        # special-case ceph 2.y branch names
        version = 2
    return '%s-%s-rhel-7' % (name, version)


def build_container(repo_url, branch, parent_image, configp):
    """ Build a container with Koji. """
    kconf = dict(configp.items('koji', vars={'branch': branch}))
    koji = KojiBuilder(hub=kconf['hub'],
                       web=kconf['web'],
                       krbservice=kconf['krbservice'])
    parent = None
    if parent_image:
        registry_url = config.lookup(configp, 'registry', 'url')
        registry = Registry(registry_url)
        parent = registry.build(parent_image)  # bucko.build.Build
    log.info('Building container at %s' % kconf['hub'])
    task_id = koji.build_container(scm=kconf['scm'],
                                   target=kconf['target'],
                                   branch=branch,
                                   repos=[repo_url],
                                   scratch=True,
                                   koji_parent_build=parent)
    # Show information to the console.
    koji.watch_task(task_id)

    # Return information about this build.
    result = {'koji_task': task_id}

    # Collapse "repositories" to "repository" if there was only one for
    # simplicity.
    repositories = koji.get_repositories(task_id)
    if len(repositories) == 1:
        result['repository'] = repositories[0]
    else:
        result['repositories'] = repositories

    return result


def parse_args():
    """ Return parsed cmdline arguments. """
    parser = argparse.ArgumentParser()
    parser.add_argument('--compose', required=False,
                        default=compose_url_from_env(),
                        help='HTTP(S) URL to a product Pungi compose.')
    return parser.parse_args()


def main():
    """ Scratch-build a container for an HTTP-accessible compose. """
    args = parse_args()

    if args.compose is None:
        err = 'Please set the CI_MESSAGE env var or use --compose arg'
        raise SystemExit(err)
    compose_url = args.compose

    # Load config file
    configp = config.load()

    # Load compose
    c = get_compose(compose_url, configp)

    # Generate .repo file
    log.info('Generating .repo file for %s compose' % c.info.release.short)
    filename = c.write_yum_repo_file()

    # Publish the .repo file
    p = get_publisher(configp)
    log.info('Publishing .repo file to %s' % p.push_url)
    repo_url = p.publish(filename)
    log.info('Published %s' % repo_url)

    # Determine scm and brew target branch name
    branch = get_branch(c)

    # Do a Koji build
    metadata = build_container(repo_url, branch,
                               c.info.base_product.parent_image, configp)

    # Store and publish our information about this build
    metadata['compose_url'] = compose_url
    metadata['compose_id'] = c.info.compose.id
    json_file = write_metadata_file(c.info.compose.id + '-osbs.json',
                                    **metadata)
    json_url = p.publish(json_file)
    log.info('OSBS JSON data at %s' % json_url)
    write_props_file(**metadata)


class BuckoError(Exception):
    pass
