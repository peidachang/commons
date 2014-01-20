# ==================================================================================================
# Copyright 2012 Twitter, Inc.
# --------------------------------------------------------------------------------------------------
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this work except in compliance with the License.
# You may obtain a copy of the License in the LICENSE file, or at:
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==================================================================================================

from __future__ import print_function

import ast
from collections import defaultdict
import itertools
import os
import pprint
import subprocess
import sys
import time

from twitter.common.collections import OrderedSet
from twitter.common.contextutil import pushd
from twitter.common.dirutil import safe_delete, safe_rmtree
from twitter.common.dirutil.chroot import Chroot
from twitter.pants.base import (
    Address,
    Config,
    Target,
    TargetDefinitionException,
)
from twitter.pants.python.antlr_builder import PythonAntlrBuilder
from twitter.pants.python.thrift_builder import PythonThriftBuilder
from twitter.pants.targets import (
    PythonAntlrLibrary,
    PythonBinary,
    PythonRequirement,
    PythonTarget,
    PythonThriftLibrary,
)

from . import Command


SETUP_BOILERPLATE = """
# DO NOT EDIT THIS FILE -- AUTOGENERATED BY PANTS
# Target: %(setup_target)s

from setuptools import setup

setup(**
%(setup_dict)s
)
"""


class SetupPy(Command):
  """Generate setup.py-based Python projects from python_library targets."""

  DECLARE_NAMESPACE = '__import__("pkg_resources").declare_namespace(__name__)'
  GENERATED_TARGETS = {
      PythonAntlrLibrary: PythonAntlrBuilder,
      PythonThriftLibrary: PythonThriftBuilder,
  }
  SOURCE_ROOT = 'src'
  __command__ = 'setup_py'

  @classmethod
  def minified_dependencies(cls, root_target):
    """Minify the dependencies of a PythonTarget.

       The algorithm works in the following fashion:

         1. Recursively resolve every dependency starting at root_target (the thing
            that setup_py is being called against).  This includes the dependencies
            of any binaries attached to the PythonArtifact using with_binaries
         2. For every PythonTarget that provides a PythonArtifact, add an
            entry for it to depmap[], keyed on the artifact name, containing
            an OrderedSet of all transitively resolved children
            dependencies.
         3. Any concrete target with sources that is provided by another PythonArtifact
            other than the one being built with setup_py will be elided.

       Downsides:
         - Explicitly requested dependencies may be elided if transitively included by others,
           e.g.
             python_library(
               ...,
               dependencies = [
                  pants('src/python/twitter/common/dirutil'),
                  pants('src/python/twitter/common/python'),
               ]
            )
          will result in only twitter.common.python being exported even if top-level sources
          directly reference twitter.common.dirutil, which could be considered a leak.
    """
    depmap = defaultdict(OrderedSet)
    providers = []

    def combined_dependencies(target):
      dependencies = getattr(target, 'dependencies', OrderedSet())
      if isinstance(target, PythonTarget) and target.provides:
        return dependencies | OrderedSet(target.provides.binaries.values())
      else:
        return dependencies

    def resolve(target, parents=None):
      parents = parents or set()
      if isinstance(target, PythonTarget) and target.provides:
        providers.append(target.provides.key)
      for dependency in combined_dependencies(target):
        for prv in providers:
          for dep in dependency.resolve():
            depmap[prv].add(dep)
            if dep in parents:
              raise TargetDefinitionException(root_target,
                 '%s and %s combined have a cycle!' % (root_target, dep))
            parents.add(dep)
            resolve(dep, parents)
            parents.remove(dep)
      if isinstance(target, PythonTarget) and target.provides:
        assert providers[-1] == target.provides.key
        providers.pop()

    resolve(root_target)
    root_deps = depmap.pop(root_target.provides.key, {})

    def elide(target):
      if any(target in depset for depset in depmap.values()):
        root_deps.discard(target)

    root_target.walk(elide)
    return root_deps

  @classmethod
  def iter_entry_points(cls, target):
    """Yields the name, entry_point pairs of binary targets in this PythonArtifact."""
    for name, binary_target in target.provides.binaries.items():
      concrete_target = binary_target.get()
      if not isinstance(concrete_target, PythonBinary) or concrete_target.entry_point is None:
        raise TargetDefinitionException(target,
            'Cannot add a binary to a PythonArtifact if it does not contain an entry_point.')
      yield name, concrete_target.entry_point

  @classmethod
  def declares_namespace_package(cls, filename):
    """Given a filename, walk its ast and determine if it is declaring a namespace package.
       Intended only for __init__.py files though it will work for any .py."""
    with open(filename) as fp:
      init_py = ast.parse(fp.read(), filename)
    calls = [node for node in ast.walk(init_py) if isinstance(node, ast.Call)]
    for call in calls:
      if len(call.args) != 1:
        continue
      if isinstance(call.func, ast.Attribute) and call.func.attr != 'declare_namespace':
        continue
      if isinstance(call.func, ast.Name) and call.func.id != 'declare_namespace':
        continue
      if isinstance(call.args[0], ast.Name) and call.args[0].id == '__name__':
        return True
    return False

  @classmethod
  def iter_generated_sources(cls, target, root, config=None):
    config = config or Config.load()
    # This is sort of facepalmy -- python.new will make this much better.
    for target_type, target_builder in cls.GENERATED_TARGETS.items():
      if isinstance(target, target_type):
        builder_cls = target_builder
        break
    else:
      raise TypeError(
          'write_generated_sources could not find suitable code generator for %s' % type(target))

    builder = builder_cls(target, root, config)
    builder.generate()
    for root, _, files in os.walk(builder.package_root):
      for fn in files:
        target_file = os.path.join(root, fn)
        yield os.path.relpath(target_file, builder.package_root), target_file

  @classmethod
  def nearest_subpackage(cls, package, all_packages):
    """Given a package, find its nearest parent in all_packages."""
    def shared_prefix(candidate):
      zipped = itertools.izip(package.split('.'), candidate.split('.'))
      matching = itertools.takewhile(lambda pair: pair[0] == pair[1], zipped)
      return [pair[0] for pair in matching]
    shared_packages = list(filter(None, map(shared_prefix, all_packages)))
    return '.'.join(max(shared_packages, key=len)) if shared_packages else package

  @classmethod
  def find_packages(cls, chroot):
    """Detect packages, namespace packages and resources from an existing chroot.

       Returns a tuple of:
         set(packages)
         set(namespace_packages)
         map(package => set(files))
    """
    base = os.path.join(chroot.path(), cls.SOURCE_ROOT)
    packages, namespace_packages = set(), set()
    resources = defaultdict(set)

    def iter_files():
      for root, _, files in os.walk(base):
        module = os.path.relpath(root, base).replace(os.path.sep, '.')
        for filename in files:
          yield module, filename, os.path.join(root, filename)

    # establish packages, namespace packages in first pass
    for module, filename, real_filename in iter_files():
      if filename != '__init__.py':
        continue
      packages.add(module)
      if cls.declares_namespace_package(real_filename):
        namespace_packages.add(module)

    # second pass establishes non-source content (resources)
    for module, filename, real_filename in iter_files():
      if filename.endswith('.py'):
        if module not in packages:
          # TODO(wickman) Consider changing this to a full-on error as it
          # could indicate bad BUILD hygiene.
          # raise cls.UndefinedSource('%s is source but does not belong to a package!' % filename)
          print('WARNING!  %s is source but does not belong to a package!' % real_filename)
        else:
          continue
      submodule = cls.nearest_subpackage(module, packages)
      if submodule == module:
        resources[submodule].add(filename)
      else:
        assert module.startswith(submodule + '.')
        relative_module = module[len(submodule) + 1:]
        relative_filename = os.path.join(relative_module.replace('.', os.path.sep), filename)
        resources[submodule].add(relative_filename)

    return packages, namespace_packages, resources

  def setup_parser(self, parser, args):
    parser.set_usage("\n"
                     "  %prog setup_py (options) [spec]\n")
    parser.add_option("--run", dest="run", default=None,
                      help="The command to run against setup.py.  Don't forget to quote "
                           "any additional parameters.  If no run command is specified, "
                           "pants will by default generate and dump the source distribution.")

  def __init__(self, run_tracker, root_dir, parser, argv):
    Command.__init__(self, run_tracker, root_dir, parser, argv)

    if not self.args:
      self.error("A spec argument is required")

    self._config = Config.load()
    self._root = root_dir

    address = Address.parse(root_dir, self.args[0])
    self.target = Target.get(address)
    if self.target is None:
      self.error('%s is not a valid target!' % self.args[0])

    if not self.target.provides:
      self.error('Target must provide an artifact.')

  def write_contents(self, chroot):
    """Write contents of the target."""
    def write_target_source(target, src):
      chroot.link(os.path.join(target.target_base, src), os.path.join(self.SOURCE_ROOT, src))
      # check parent __init__.pys to see if they also need to be linked.  this is to allow
      # us to determine if they belong to regular packages or namespace packages.
      while True:
        src = os.path.dirname(src)
        if not src:
          # Do not allow the repository root to leak (i.e. '.' should not be a package in setup.py)
          break
        if os.path.exists(os.path.join(target.target_base, src, '__init__.py')):
          chroot.link(os.path.join(target.target_base, src, '__init__.py'),
                      os.path.join(self.SOURCE_ROOT, src, '__init__.py'))

    def write_codegen_source(relpath, abspath):
      chroot.link(abspath, os.path.join(self.SOURCE_ROOT, relpath))

    def write_target(target):
      if isinstance(target, tuple(self.GENERATED_TARGETS.keys())):
        for relpath, abspath in self.iter_generated_sources(target, self._root, self._config):
          write_codegen_source(relpath, abspath)
      else:
        for source in list(target.sources) + list(target.resources):
          write_target_source(target, source)

    write_target(self.target)
    for dependency in self.minified_dependencies(self.target):
      if isinstance(dependency, PythonTarget) and not dependency.provides:
        write_target(dependency)

  def write_setup(self, chroot):
    """Write the setup.py of a target.  Must be run after writing the contents to the chroot."""
    setup_keywords = self.target.provides.setup_py_keywords

    package_dir = {'': self.SOURCE_ROOT}
    packages, namespace_packages, resources = self.find_packages(chroot)

    if namespace_packages:
      setup_keywords['namespace_packages'] = list(sorted(namespace_packages))

    if packages:
      setup_keywords.update(
          package_dir=package_dir,
          packages=list(sorted(packages)),
          package_data=dict((package, list(rs)) for (package, rs) in resources.items()))

    install_requires = set()
    for dep in self.minified_dependencies(self.target):
      if isinstance(dep, PythonRequirement):
        install_requires.add(str(dep.requirement))
      elif isinstance(dep, PythonTarget) and dep.provides:
        install_requires.add(dep.provides.key)
    setup_keywords['install_requires'] = list(install_requires)

    for binary_name, entry_point in self.iter_entry_points(self.target):
      if 'entry_points' not in setup_keywords:
        setup_keywords['entry_points'] = {}
      if 'console_scripts' not in setup_keywords['entry_points']:
        setup_keywords['entry_points']['console_scripts'] = []
      setup_keywords['entry_points']['console_scripts'].append(
          '%s = %s' % (binary_name, entry_point))

    chroot.write(SETUP_BOILERPLATE % {
      'setup_dict': pprint.pformat(setup_keywords, indent=4),
      'setup_target': repr(self.target)
    }, 'setup.py')

  def execute(self):
    dist_dir = self._config.getdefault('pants_distdir')
    target_base = '%s-%s' % (
        self.target.provides.name, self.target.provides.version)
    setup_dir = os.path.join(dist_dir, target_base)
    expected_tgz = '%s.tar.gz' % target_base
    expected_target = os.path.join(setup_dir, 'dist', expected_tgz)
    dist_tgz = os.path.join(dist_dir, expected_tgz)

    chroot = Chroot(dist_dir, name=self.target.provides.name)
    self.write_contents(chroot)
    self.write_setup(chroot)
    safe_rmtree(setup_dir)
    os.rename(chroot.path(), setup_dir)

    with pushd(setup_dir):
      cmd = '%s setup.py %s' % (sys.executable, self.options.run or 'sdist')
      print('Running "%s" in %s' % (cmd, setup_dir))
      extra_args = {} if self.options.run else dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
      po = subprocess.Popen(cmd, shell=True, **extra_args)
      stdout, stderr = po.communicate()

    if self.options.run:
      print('Ran %s' % cmd)
      print('Output in %s' % setup_dir)
      return po.returncode
    elif po.returncode != 0:
      print('Failed to run %s!' % cmd)
      for line in ''.join(stdout).splitlines():
        print('stdout: %s' % line)
      for line in ''.join(stderr).splitlines():
        print('stderr: %s' % line)
      return po.returncode
    else:
      if not os.path.exists(expected_target):
        print('Could not find expected target %s!' % expected_target)
        sys.exit(1)

      safe_delete(dist_tgz)
      os.rename(expected_target, dist_tgz)
      safe_rmtree(setup_dir)

      print('Wrote %s' % dist_tgz)
