# coding=utf-8
from __future__ import absolute_import, unicode_literals, print_function, division

import os

from flask import current_app, url_for, safe_join
from werkzeug.exceptions import NotFound, BadRequest
from pygit2 import GIT_OBJ_BLOB, GIT_OBJ_TREE, GIT_BLAME_TRACK_COPIES_SAME_COMMIT_MOVES, GIT_BLAME_TRACK_COPIES_SAME_COMMIT_COPIES, GIT_SORT_NONE

from restfulgit.plumbing.retrieval import lookup_ref, get_commit
from restfulgit.plumbing.converters import GIT_OBJ_TYPE_TO_NAME, encode_blob_data


DEFAULT_GIT_DESCRIPTION = "Unnamed repository; edit this file 'description' to name the repository.\n"
GIT_OBJ_TO_PORCELAIN_NAME = {
    GIT_OBJ_TREE: 'dir',
    GIT_OBJ_BLOB: 'file',
}


def get_repo_names():
    children = (
        (name, safe_join(current_app.config['RESTFULGIT_REPO_BASE_PATH'], name))
        for name in os.listdir(current_app.config['RESTFULGIT_REPO_BASE_PATH'])
    )
    subdirs = [(dir_name, full_path) for dir_name, full_path in children if os.path.isdir(full_path)]
    mirrors = set(name for name, _ in subdirs if name.endswith('.git'))
    working_copies = set(name for name, full_path in subdirs if os.path.isdir(safe_join(full_path, '.git')))
    repositories = mirrors | working_copies
    return repositories


def get_commit_for_refspec(repo, branch_or_tag_or_sha):
    # Precedence order GitHub uses (& that we copy):
    # 1. Branch  2. Tag  3. Commit SHA
    commit_sha = None
    # branch?
    branch_ref = lookup_ref(repo, branch_or_tag_or_sha)
    if branch_ref is not None:
        commit_sha = branch_ref.resolve().target.hex
    # tag?
    if commit_sha is None:
        ref_to_tag = lookup_ref(repo, "tags/" + branch_or_tag_or_sha)
        if ref_to_tag is not None:
            commit_sha = ref_to_tag.get_object().hex
    # commit?
    if commit_sha is None:
        commit_sha = branch_or_tag_or_sha
    try:
        return get_commit(repo, commit_sha)
    except (ValueError, NotFound):
        raise NotFound("no such branch, tag, or commit SHA")


def get_branch(repo, branch_name):
    branch = repo.lookup_branch(branch_name)
    if branch is None:
        raise NotFound("branch not found")
    return branch


def get_object_from_path(repo, tree, path):
    path_segments = path.split("/")

    ctree = tree
    for i, path_seg in enumerate(path_segments):
        if ctree.type != GIT_OBJ_TREE:
            raise NotFound("invalid path; traversal unexpectedly encountered a non-tree")
        if not path_seg and i == len(path_segments) - 1:  # allow trailing slash in paths to directories
            continue
        try:
            ctree = repo[ctree[path_seg].oid]
        except KeyError:
            raise NotFound("invalid path; no such object")
    return ctree


def get_repo_description(repo_key):
    relative_paths = (
        os.path.join(repo_key, 'description'),
        os.path.join(repo_key, '.git', 'description'),
    )
    extant_relative_paths = (
        relative_path
        for relative_path in relative_paths
        if os.path.isfile(safe_join(current_app.config['RESTFULGIT_REPO_BASE_PATH'], relative_path))
    )
    extant_relative_path = next(extant_relative_paths, None)
    if extant_relative_path is None:
        return None
    with open(os.path.join(current_app.config['RESTFULGIT_REPO_BASE_PATH'], extant_relative_path), 'r') as content_file:
        description = content_file.read()
        if description == DEFAULT_GIT_DESCRIPTION:
            description = None
        return description


def get_raw_file_content(repo, tree, path):
    git_obj = get_object_from_path(repo, tree, path)
    if git_obj.type != GIT_OBJ_BLOB:
        raise BadRequest("path resolved to non-blob object")
    return git_obj.data


def get_diff(repo, commit):
    if commit.parents:
        diff = repo.diff(commit.parents[0], commit)
        diff.find_similar()
    else:  # NOTE: RestfulGit extension; GitHub gives a 404 in this case
        diff = commit.tree.diff_to_tree(swap=True)
    return diff


def get_blame(repo, file_path, newest_commit, oldest_refspec=None, min_line=1, max_line=None):  # pylint: disable=R0913
    kwargs = {
        'flags': (GIT_BLAME_TRACK_COPIES_SAME_COMMIT_MOVES | GIT_BLAME_TRACK_COPIES_SAME_COMMIT_COPIES),
        'newest_commit': newest_commit.oid,
    }
    if oldest_refspec is not None:
        oldest_commit = get_commit_for_refspec(repo, oldest_refspec)
        kwargs['oldest_commit'] = oldest_commit.oid
    if min_line > 1:
        kwargs['min_line'] = min_line
    if max_line is not None:
        kwargs['max_line'] = max_line

    try:
        return repo.blame(file_path, **kwargs)
    except KeyError as no_such_file_err:
        raise NotFound(no_such_file_err.message)
    except ValueError:
        raise BadRequest("path resolved to non-blob object")


def get_authors(repo):
    return (commit.author for commit in repo.walk(repo.head.target, GIT_SORT_NONE))  # pylint: disable=E1103


# FIX ME: should be in different module?
def get_contents(repo_key, repo, refspec, file_path, obj, _recursing=False):
    # FIX ME: implement symlink and submodule cases
    if not _recursing and obj.type == GIT_OBJ_TREE:
        entries = [
            get_contents(repo_key, repo, refspec, os.path.join(file_path, entry.name), repo[entry.oid], _recursing=True)
            for entry in obj
        ]
        entries.sort(key=lambda entry: entry["name"])
        return entries

    contents_url = url_for('porcelain.get_contents', _external=True, repo_key=repo_key, file_path=file_path, ref=refspec)
    git_url = url_for('plumbing.get_' + GIT_OBJ_TYPE_TO_NAME[obj.type], _external=True, repo_key=repo_key, sha=obj.hex)

    result = {
        "type": GIT_OBJ_TO_PORCELAIN_NAME[obj.type],
        "sha": obj.hex,
        "name": os.path.basename(file_path),
        "path": file_path,
        "size": (obj.size if obj.type == GIT_OBJ_BLOB else 0),
        "url": contents_url,
        "git_url": git_url,
        "_links": {
            "self": contents_url,
            "git": git_url,
        }
    }
    if not _recursing and obj.type == GIT_OBJ_BLOB:
        encoding, data = encode_blob_data(obj.data)
        result["encoding"] = encoding
        result["content"] = data
    return result
