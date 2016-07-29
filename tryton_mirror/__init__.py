import os
import cmd
import shlex
import subprocess
import ConfigParser

import hgapi
import requests
from github import Github

# The repositories that need to be mirrored.
# The format:
#
#  ('relative path of tryton repo', 'git_repo_name')
REPOS = []

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
GITHUB_ORG = os.environ.get('GITHUB_ORG', BB_OWNER)
GITHUB_USER = os.environ.get('GITHUB_USER', '')
GITHUB_PASSWD = os.environ.get('GITHUB_PASSWORD', '')


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
                subprocess.call(
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
            ret = subprocess.call(shlex.split(cmd))
            if ret != 0:
                continue

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
            subprocess.call(
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
            if not os.path.exists(os.path.join(HG_CACHE, hg_module)):
                continue
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
        return "git@github.com:%s/%s.git" % (GITHUB_ORG, git_name)

    def do_push_to_remotes(self, line=None):
        """
        Push the code to the remotes in a git repository
        """
        for hg_module, _ in REPOS:
            remotes = [self._get_default_remote(hg_module)]
            remotes.extend(ADDITIONAL_REMOTES.get('git_name', []))
            for remote in remotes:
                subprocess.call(
                    shlex.split(
                        'git --git-dir=%s/%s/.git push -q --mirror %s' % (
                            GIT_CACHE, hg_module, remote)))


class RepoHandler(object):
    namespace_id = None
    github_client = None

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
                # Skip deprecated modules
                project_key = repo.get('project', {}).get('key', '')
                if project_key == 'DEP':
                    continue
                if repo['scm'] != 'hg':
                    continue
                # Skip not tryton nor python modules
                if name[:8] != 'trytond-' and name[:7] != 'python-':
                    continue
                url, = [r['href'] for r in repo['links']['clone']
                    if r['name'] == 'https']
                repos.append((repo['name'], url))
            page += 1
        return repos

    def get_github_client(self):
        """
        Return an authenticated github client
        """
        if self.github_client:
            return self.github_client

        self.github_client = Github(GITHUB_USER, GITHUB_PASSWD)
        return self.github_client

    def create_repo(self, repo_name, homepage=None):
        github_client = self.get_github_client()
        org = github_client.get_organization(GITHUB_ORG)
        return org.create_repo(repo_name, 'Mirror of %s' % repo_name,
                    homepage=homepage, has_wiki=False, has_issues=False)

    def create_missing_repos(self):
        repos = [x[0] for x in self.get_bitbucket_owner_modules()]

        github_client = self.get_github_client()
        tryton_org = github_client.get_organization(GITHUB_ORG)
        org_repos = {r.name: r for r in tryton_org.get_repos()}
        for repo_name in repos:
            homepage = 'https://bitbucket.org/%s/%s' % (BB_OWNER,
                repo_name)
            repo = org_repos.get(repo_name)
            if not repo:
                self.create_repo(repo_name, homepage)
            elif repo.has_wiki or repo.has_issues or repo.homepage != homepage:
                repo.edit(repo_name, homepage=homepage,
                    has_wiki=False, has_issues=False)


# Add the modules from tryonspain
REPOS += RepoHandler.get_bitbucket_owner_modules()


if __name__ == '__main__':
    CommandHandler().cmdloop()
