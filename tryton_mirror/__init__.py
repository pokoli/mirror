import os
import cmd
import shlex
import subprocess
import json
import ConfigParser

import hgapi
import requests

# The repositories that need to be mirrored.
# The format:
#
#  ('relative path of tryton repo', 'git_repo_name')
REPOS = []

# Canonical source base_url
GITLAB_HOST = os.environ.get('GITLAB_HOST', 'localhost')
GITLAB_URL = 'http://%s/api/v3/' % GITLAB_HOST
GITLAB_TOKEN = os.environ.get('GITLAB_TOKEN', '')

# The directory where the mercurial repos should be cloned to
HG_CACHE = os.environ.get('HG_CACHE', 'hg')

# The directory where git repositories should be cached
GIT_CACHE = os.environ.get('GIT_CACHE', 'git')

# additional git remotes. A provision to set the remotes other than the
# default github remote
ADDITIONAL_REMOTES = {
    # module: [list, of, remotes]
}

BB_OWNER = os.environ.get('BB_OWNER', 'trytonspain')


class CommandHandler(cmd.Cmd):

    def do_setup(self, line=None):
        """
        Setup the cache folders

        * Setup cache folders
        * Setup empty git repos for each module
        """
        if not os.path.exists(HG_CACHE):
            os.makedirs(HG_CACHE)
        if not os.path.exists(GIT_CACHE):
            os.makedirs(GIT_CACHE)

        for hg_module, _ in REPOS:
            git_repo_dir = os.path.join(GIT_CACHE, hg_module)
            if not os.path.exists(git_repo_dir):
                subprocess.check_call(
                    shlex.split('git init -q %s' % git_repo_dir))

    def do_clone_all(self, line=None):
        """
        Clone all hg repos
        """
        for hg_module, repo_url in REPOS:
            if os.path.exists(os.path.join(HG_CACHE, hg_module)):
                continue

            cmd = 'hg clone -q %s %s/%s' % (
                repo_url, HG_CACHE, hg_module,
            )
            subprocess.check_call(shlex.split(cmd))

            hgrc = os.path.join('.', HG_CACHE, hg_module, '.hg/hgrc')

            config = ConfigParser.ConfigParser()
            config.readfp(open(hgrc))

            # Set the configuration for extensions and bookmarks
            if 'extensions' not in config.sections():
                config.add_section('extensions')
            config.set('extensions', 'hgext.bookmarks', '')
            config.set('extensions', 'hggit', '')

            # Setting for using named branches
            # https://github.com/schacon/hg-git#gitbranch_bookmark_suffix
            if 'git' not in config.sections():
                config.add_section('git')
            config.set('git', 'branch_bookmark_suffix', '_bookmark')

            with open(hgrc, 'wb') as configfile:
                config.write(configfile)

    def do_pull_all(self, line=None):
        """
        Pull all repos one by one
        """
        for hg_module, repo_url in REPOS:
            subprocess.check_call(
                shlex.split('hg --cwd %s pull -u -q' %
                    os.path.join(HG_CACHE, hg_module))
                )

    def _make_bookmarks(self, repo):
        """
        Create bookmarks for each repo
        """
        for branch in repo.get_branch_names():
            bookmark = '%s_bookmark' % branch
            if branch == 'default':
                bookmark = 'develop_bookmark'
            repo.hg_command('bookmark', '-f', '-r', branch, bookmark)

    def do_hg_to_git(self, line=None):
        """
        Move from hg to local git repo
        """
        for hg_module, _ in REPOS:
            hg_repo = hgapi.Repo(os.path.join(HG_CACHE, hg_module))
            self._make_bookmarks(hg_repo)
            cmd = shlex.split('hg --cwd=%s push -q %s' % (
                    os.path.join(HG_CACHE, hg_module),
                    os.path.abspath(os.path.join(GIT_CACHE, hg_module))
                    ))
            retcode = subprocess.call(cmd)
            if retcode not in [0, 1]:
                raise subprocess.CalledProcessError(retcode, cmd)

    def _get_default_remote(self, git_name):
        return "git@%s:%s/%s.git" % (GITLAB_HOST, BB_OWNER, git_name)

    def do_push_to_remotes(self, line=None):
        """
        Push the code to the remotes in a git repository
        """
        for hg_module, _ in REPOS:
            remotes = [self._get_default_remote(hg_module)]
            remotes.extend(ADDITIONAL_REMOTES.get('git_name', []))
            for remote in remotes:
                subprocess.check_call(
                    shlex.split(
                        'git --git-dir=%s/%s/.git push -q --mirror %s' % (
                            GIT_CACHE, hg_module, remote)))


class RepoHandler(object):

    namespace_id = None

    @staticmethod
    def get_bitbucket_owner_modules():
        base = ('https://api.bitbucket.org/2.0/repositories/%s/?pagelen=100' %
            BB_OWNER)
        repos = []
        size = None
        processed = 0
        page = 1
        while size is None or processed < size:
            rv = requests.get('%s&page=%d' % (base, page))
            data = rv.json()
            if size is None:
                size = data['size']
            for repo in data['values']:
                processed += 1
                name = repo['name']
                # Skip not tryton modules
                if name[:8] != 'trytond-' or repo['scm'] != 'hg':
                    continue
                url, = [r['href'] for r in repo['links']['clone']
                    if r['name'] == 'https']
                repos.append((repo['name'], url))
            page += 1
        return repos

    def create_repo(self, repo_name, homepage=None):
        if self.namespace_id is None:
            rv = requests.get(
                '%s/namespaces?search=%s&private_token=%s' % (
                    GITLAB_URL, BB_OWNER, GITLAB_TOKEN))
            self.namespace_id = rv.json()[0]['id']
        project_data = {
            'name': repo_name,
            'description': 'CI mirror of %s' % (repo_name),
            'namespace_id': self.namespace_id,
            'public': True,
            'issues_enabled': False,
            'merge_requests_enabled': False,
            'wiki_enabled': False,
            }
        headers = {'Content-Type': 'application/json'}
        url = '%s/projects?&private_token=%s' % (GITLAB_URL, GITLAB_TOKEN)
        rv = requests.post(url, data=json.dumps(project_data), headers=headers)

    def create_missing_repos(self):
        repos = [x[0] for x in self.get_bitbucket_owner_modules()]

        rv = requests.get('%s/projects?private_token=%s' % (
                GITLAB_URL, GITLAB_TOKEN))
        git_repos = [r['name'] for r in rv.json()]
        for repo_name in repos:
            if repo_name not in git_repos:
                self.create_repo(repo_name)

# Add the modules from tryonspain
REPOS += RepoHandler.get_bitbucket_owner_modules()


if __name__ == '__main__':
    CommandHandler().cmdloop()
