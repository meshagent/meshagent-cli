import typer
from typing import Annotated, Optional
from rich import print
import os
import fnmatch
import glob
import shutil

from meshagent.api import RoomClient, WebSocketClientProtocol
from meshagent.api.room_server_client import StorageClient
from meshagent.api.helpers import meshagent_base_url, websocket_room_url
from meshagent.cli import async_typer
from meshagent.cli.helper import (
    get_client,
    resolve_project_id,
    resolve_api_key,
    resolve_token_jwt,
)
from meshagent.cli.helper import resolve_room

app = async_typer.AsyncTyper()


def parse_path(path: str):
    """
    Parse a path and return a tuple (scheme, subpath).
    scheme = "room" if it starts with "room://", otherwise "local".
    subpath = the remainder of the path with no leading slashes.
    """
    prefix = "room://"
    if path.startswith(prefix):
        return ("room", path[len(prefix) :])
    return ("local", path)


def split_glob_subpath(subpath: str):
    """
    Given something like "folder/*.txt" or "folder/subfolder/*.json",
    return (dir_part, pattern_part).

    If there's no wildcard, we return (subpath, None).
    """
    # If there is no '*', '?', or '[' in subpath, we assume no glob
    # (simplistic check — if you want more robust detection, parse carefully).
    if any(c in subpath for c in ["*", "?", "["]):
        # We assume the pattern is the final portion after the last slash
        # e.g. "folder/*.txt" => dir_part="folder", pattern_part="*.txt"
        # If there's no slash, dir_part="" and pattern_part=subpath
        base_dir = os.path.dirname(subpath)
        pattern = os.path.basename(subpath)
        return (base_dir, pattern)
    else:
        return (subpath, None)


@app.async_command("exists")
async def storage_exists_command(
    *,
    project_id: str = None,
    room: Annotated[str, typer.Option(..., help="Room name")],
    token_path: Annotated[Optional[str], typer.Option()] = None,
    api_key_id: Annotated[Optional[str], typer.Option(..., help="API Key ID")] = None,
    name: Annotated[str, typer.Option(..., help="Participant name")] = "cli",
    role: str = "user",
    path: str,
):
    """
    Check if a file/folder exists in remote storage.
    """
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        api_key_id = await resolve_api_key(project_id, api_key_id)
        room = resolve_room(room)
        jwt = await resolve_token_jwt(
            project_id=project_id,
            api_key_id=api_key_id,
            token_path=token_path,
            name=name,
            role=role,
            room=room,
        )

        print("[bold green]Connecting to room...[/bold green]")
        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room, base_url=meshagent_base_url()),
                token=jwt,
            )
        ) as client:
            file_exists = await client.storage.exists(path=path)
            if file_exists:
                print(f"[bold cyan]'{path}' exists in remote storage.[/bold cyan]")
            else:
                print(
                    f"[bold red]'{path}' does NOT exist in remote storage.[/bold red]"
                )
    finally:
        await account_client.close()


@app.async_command("cp")
async def storage_cp_command(
    *,
    project_id: str = None,
    room: Annotated[
        str, typer.Option(..., help="Room name (if copying to/from remote)")
    ],
    token_path: Annotated[Optional[str], typer.Option()] = None,
    api_key_id: Annotated[
        str, typer.Option(..., help="API Key ID (if copying to/from remote)")
    ] = None,
    name: Annotated[
        str, typer.Option(..., help="Participant name (if copying to/from remote)")
    ] = "cli",
    role: str = "user",
    source_path: str,
    dest_path: str,
):
    room = resolve_room(room)

    try:
        """
        Copy files between local and remote storage. Supports globs on the source side.
        - cp room://folder/*.txt mylocaldir
        - cp *.txt room://myfolder
        - cp room://file.bin room://backup/file.bin
        - etc.
        """
        # Determine what is remote or local for source/dest
        src_scheme, src_subpath = parse_path(source_path)
        dst_scheme, dst_subpath = parse_path(dest_path)

        # This code will only connect to the room once if either side is remote
        # (or both are remote).
        account_client = None
        client = None
        storage_client: None | StorageClient = None

        # A helper to ensure we have a connected StorageClient if needed
        async def ensure_storage_client():
            nonlocal account_client, client, storage_client, api_key_id, project_id

            if storage_client is not None:
                return  # Already connected

            account_client = await get_client()
            project_id = await resolve_project_id(project_id=project_id)
            api_key_id = await resolve_api_key(project_id, api_key_id)

            jwt = await resolve_token_jwt(
                project_id=project_id,
                api_key_id=api_key_id,
                token_path=token_path,
                name=name,
                role=role,
                room=room,
            )

            print("[bold green]Connecting to room...[/bold green]")
            client = RoomClient(
                protocol=WebSocketClientProtocol(
                    url=websocket_room_url(
                        room_name=room, base_url=meshagent_base_url()
                    ),
                    token=jwt,
                )
            )

            await client.__aenter__()  # Manually enter the async context
            storage_client = client.storage

        # We’ll gather the final (source->destination) file pairs to copy
        copy_operations = []

        # 1) Expand the source side if there's a glob
        def local_list_files_with_glob(path_pattern: str):
            return glob.glob(path_pattern)

        async def remote_list_files_with_glob(sc: StorageClient, path_pattern: str):
            base_dir, maybe_pattern = split_glob_subpath(path_pattern)
            if maybe_pattern is None:
                # Single file
                # We'll just see if it exists
                file_exists = await sc.exists(path=base_dir)
                if not file_exists:
                    raise FileNotFoundError(
                        f"Remote file '{path_pattern}' does not exist."
                    )
                return [(base_dir, os.path.basename(base_dir))]  # (full, filename)
            else:
                # List base_dir, filter
                entries = await sc.list(path=base_dir)
                matched = [
                    (os.path.join(base_dir, e.name), e.name)
                    for e in entries
                    if not e.is_folder and fnmatch.fnmatch(e.name, maybe_pattern)
                ]
                return matched

        # Expand the source files
        if src_scheme == "local":
            # local
            if any(c in src_subpath for c in ["*", "?", "["]):
                # Glob
                local_matches = local_list_files_with_glob(src_subpath)
                if not local_matches:
                    print(f"[bold red]No local files match '{src_subpath}'[/bold red]")
                    return
                # We'll store (absolute_source_file, filename_only)
                expanded_sources = [
                    (m, os.path.basename(m)) for m in local_matches if os.path.isfile(m)
                ]
            else:
                # Single local file
                if not os.path.isfile(src_subpath):
                    print(f"[bold red]Local file '{src_subpath}' not found.[/bold red]")
                    return
                expanded_sources = [(src_subpath, os.path.basename(src_subpath))]
        else:
            # remote
            await ensure_storage_client()
            matches = await remote_list_files_with_glob(storage_client, src_subpath)
            if not matches:
                print(f"[bold red]No remote files match '{src_subpath}'[/bold red]")
                return
            expanded_sources = matches  # list of (full_remote_path, filename)

        # 2) Figure out if destination is a single file or a directory
        #    We'll handle multi-file -> directory or single-file -> single-file.
        multiple_sources = len(expanded_sources) > 1

        if dst_scheme == "local":
            # If local destination is a directory or ends with a path separator,
            # we treat it as a directory. If multiple files, it must be a directory.
            if (
                os.path.isdir(dst_subpath)
                or dst_subpath.endswith(os.sep)
                or dst_subpath == ""
            ):
                # directory
                for full_src, fname in expanded_sources:
                    copy_operations.append((full_src, os.path.join(dst_subpath, fname)))
            else:
                # single file (or maybe it doesn't exist yet, but no slash)
                if multiple_sources:
                    # Must be a directory, but user gave a file-like name => error
                    print(
                        f"[bold red]Destination '{dest_path}' is not a directory, but multiple files are being copied.[/bold red]"
                    )
                    return
                # single file
                copy_operations.append((expanded_sources[0][0], dst_subpath))
        else:
            # remote
            # We need to see if there's a slash at the end or if it might be a directory
            # There's no built-in "is_folder" check by default except listing. We'll do a naive approach:
            # We'll see if the path exists as a folder by listing it. If that fails, assume it's a file path.
            await ensure_storage_client()

            # Let's attempt to list the `dst_subpath`. If it returns any result, it might exist as a folder.
            # If it doesn't exist, we treat it as a file path.  This can get tricky if your backend
            # doesn't differentiate a folder from an empty folder. We'll keep it simple.
            is_destination_folder = False
            try:
                await storage_client.list(path=dst_subpath)
                # If listing worked, it might be a folder (unless it's a file with children?).
                # We'll assume it’s a folder if we get any results or no error.
                is_destination_folder = True
            except Exception:
                # Probably a file or does not exist
                is_destination_folder = False

            if is_destination_folder:
                # it's a folder
                for full_src, fname in expanded_sources:
                    # We'll store a path "dst_subpath/fname"
                    remote_dest_file = os.path.join(dst_subpath, fname)
                    copy_operations.append((full_src, remote_dest_file))
            else:
                # single file path
                if multiple_sources:
                    print(
                        f"[bold red]Destination '{dest_path}' is not a folder, but multiple files are being copied.[/bold red]"
                    )
                    return
                copy_operations.append((expanded_sources[0][0], dst_subpath))

        # 3) Perform the copy
        #    copy_operations is a list of (source_file, dest_file).
        #    We need to handle four combos:
        #    a) local->local (unlikely in your scenario, but we handle it)
        #    b) local->remote
        #    c) remote->local
        #    d) remote->remote
        for src_file, dst_file in copy_operations:
            # Determine combo
            if src_scheme == "local" and dst_scheme == "local":
                # local->local
                print(f"Copying local '{src_file}' -> local '{dst_file}'")
                os.makedirs(os.path.dirname(dst_file), exist_ok=True)
                with open(src_file, "rb") as fsrc, open(dst_file, "wb") as fdst:
                    fdst.write(fsrc.read())
                print(
                    f"[bold cyan]Copied local '{src_file}' to '{dst_file}'[/bold cyan]"
                )

            elif src_scheme == "local" and dst_scheme == "room":
                # local->remote
                print(f"Copying local '{src_file}' -> remote '{dst_file}'")
                with open(src_file, "rb") as fsrc:
                    data = fsrc.read()
                # open, write, close
                dest_handle = await storage_client.open(path=dst_file, overwrite=True)
                await storage_client.write(handle=dest_handle, data=data)
                await storage_client.close(handle=dest_handle)
                print(
                    f"[bold cyan]Uploaded '{src_file}' to remote '{dst_file}'[/bold cyan]"
                )

            elif src_scheme == "room" and dst_scheme == "local":
                # remote->local
                print(f"Copying remote '{src_file}' -> local '{dst_file}'")
                remote_file = await storage_client.download(path=src_file)
                if os.path.dirname(dst_file) != "":
                    os.makedirs(os.path.dirname(dst_file), exist_ok=True)
                with open(dst_file, "wb") as fdst:
                    fdst.write(remote_file.data)
                print(
                    f"[bold cyan]Downloaded remote '{src_file}' to local '{dst_file}'[/bold cyan]"
                )

            else:
                # remote->remote
                print(f"Copying remote '{src_file}' -> remote '{dst_file}'")
                source_file = await storage_client.download(path=src_file)
                dest_handle = await storage_client.open(path=dst_file, overwrite=True)
                await storage_client.write(handle=dest_handle, data=source_file.data)
                await storage_client.close(handle=dest_handle)
                print(
                    f"[bold cyan]Copied remote '{src_file}' to '{dst_file}'[/bold cyan]"
                )
    finally:
        # Clean up
        if client is not None:
            await client.__aexit__(None, None, None)
        if account_client is not None:
            await account_client.close()


@app.async_command("show")
async def storage_show_command(
    *,
    project_id: Annotated[
        Optional[str], typer.Option(..., help="Project ID (if remote)")
    ] = None,
    room: Annotated[
        Optional[str], typer.Option(..., help="Room name (if remote)")
    ] = None,
    token_path: Annotated[Optional[str], typer.Option()] = None,
    api_key_id: Annotated[
        Optional[str], typer.Option(..., help="API Key ID (if remote)")
    ] = None,
    name: Annotated[
        Optional[str], typer.Option(..., help="Participant name (if remote)")
    ] = None,
    role: str = "user",
    path: str,
    encoding: Annotated[
        str, typer.Option("--encoding", help="Text encoding")
    ] = "utf-8",
):
    """
    Print the contents of a file (local or remote) to the console.
    """
    room = resolve_room(room)

    scheme, subpath = parse_path(path)

    # If we need a remote connection, set it up:
    account_client = None
    client = None
    storage_client = None

    async def ensure_storage_client():
        nonlocal account_client, client, storage_client, api_key_id, project_id

        if storage_client is not None:
            return

        if not room:
            raise typer.BadParameter("To show a remote file, you must provide --room")

        account_client = await get_client()
        project_id = await resolve_project_id(project_id=project_id)
        api_key_id = await resolve_api_key(project_id, api_key_id)

        jwt = await resolve_token_jwt(
            project_id=project_id,
            api_key_id=api_key_id,
            token_path=token_path,
            name=name,
            role=role,
            room=room,
        )

        print("[bold green]Connecting to room...[/bold green]")
        client = RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room, base_url=meshagent_base_url()),
                token=jwt,
            )
        )

        await client.__aenter__()
        storage_client = client.storage

    try:
        if scheme == "local":
            # Local file read
            if not os.path.isfile(subpath):
                print(f"[bold red]Local file '{subpath}' not found.[/bold red]")
                raise typer.Exit(code=1)
            with open(subpath, "rb") as f:
                data = f.read()
            text_content = data.decode(encoding, errors="replace")
            print(text_content)
        else:
            # Remote file read
            await ensure_storage_client()
            if not await storage_client.exists(path=subpath):
                print(f"[bold red]Remote file '{subpath}' not found.[/bold red]")
                raise typer.Exit(code=1)
            remote_file = await storage_client.download(path=subpath)
            text_content = remote_file.data.decode(encoding, errors="replace")
            print(text_content)
    finally:
        if client is not None:
            await client.__aexit__(None, None, None)
        if account_client is not None:
            await account_client.close()


@app.async_command("rm")
async def storage_rm_command(
    *,
    project_id: Annotated[
        Optional[str], typer.Option(..., help="Project ID (if remote)")
    ] = None,
    room: Annotated[
        Optional[str], typer.Option(..., help="Room name (if remote)")
    ] = None,
    token_path: Annotated[Optional[str], typer.Option()] = None,
    api_key_id: Annotated[
        Optional[str], typer.Option(..., help="API Key ID (if remote)")
    ] = None,
    name: Annotated[
        Optional[str], typer.Option(..., help="Participant name (if remote)")
    ] = None,
    role: str = "user",
    path: str,
    recursive: Annotated[
        bool, typer.Option("-r", help="Remove directories/folders recursively")
    ] = False,
):
    """
    Remove files (and optionally folders) either locally or in remote storage.
    - Supports wildcards on the source path (for files).
    - Fails if trying to remove a directory/folder without --recursive/-r.
    """
    room = resolve_room(room)

    try:
        # We'll mimic the "cp" approach, expanding wildcards for local/remote.

        scheme, subpath = parse_path(path)

        account_client = None
        client = None
        storage_client: Optional[StorageClient] = None

        # Helper to ensure we have a storage client if we need remote operations
        async def ensure_storage_client():
            nonlocal account_client, client, storage_client, project_id, api_key_id

            if storage_client is not None:
                return  # Already set up

            if not room:
                raise typer.BadParameter(
                    "To remove a remote file or folder, you must provide --room"
                )

            account_client = await get_client()
            project_id = await resolve_project_id(project_id=project_id)
            resolved_project_id = await resolve_project_id(project_id=project_id)
            jwt = await resolve_token_jwt(
                project_id=resolved_project_id,
                api_key_id=api_key_id,
                token_path=token_path,
                name=name,
                role=role,
                room=room,
            )

            print("[bold green]Connecting to room...[/bold green]")
            client = RoomClient(
                protocol=WebSocketClientProtocol(
                    url=websocket_room_url(
                        room_name=room, base_url=meshagent_base_url()
                    ),
                    token=jwt,
                )
            )

            await client.__aenter__()
            storage_client = client.storage

        # --------------
        # LOCAL HELPERS
        # --------------
        def local_list_files_with_glob(path_pattern: str):
            return glob.glob(path_pattern)

        def remove_local_path(local_path: str, recursive: bool):
            """Remove a single local path (file or directory). Respects 'recursive' for directories."""
            if not os.path.exists(local_path):
                print(
                    f"[yellow]Local path '{local_path}' does not exist. Skipping.[/yellow]"
                )
                return

            if os.path.isfile(local_path):
                os.remove(local_path)
                print(f"[bold cyan]Removed local file '{local_path}'[/bold cyan]")
            elif os.path.isdir(local_path):
                if not recursive:
                    print(
                        f"[bold red]Cannot remove directory '{local_path}' without -r.[/bold red]"
                    )
                    raise typer.Exit(code=1)
                shutil.rmtree(local_path)
                print(
                    f"[bold cyan]Removed local directory '{local_path}' recursively[/bold cyan]"
                )
            else:
                # Neither file nor directory?
                print(
                    f"[bold red]'{local_path}' is not a regular file or directory. Skipping.[/bold red]"
                )

        # ---------------
        # REMOTE HELPERS
        # ---------------
        async def remote_list_files_with_glob(sc: StorageClient, path_pattern: str):
            """
            If there's a wildcard, returns only matching files (not folders),
            consistent with 'cp' approach. If no wildcard, returns either
            [(full_path, basename)] if it exists or empty list if not found.
            """
            base_dir, maybe_pattern = split_glob_subpath(path_pattern)
            if maybe_pattern is None:
                # Single path
                file_exists = await sc.exists(path=base_dir)
                if not file_exists:
                    return []
                # We don't know if it's file or folder yet, but for a single path, we'll treat it as one item.
                return [(base_dir, os.path.basename(base_dir))]
            else:
                # We have a pattern
                entries = await sc.list(path=base_dir)
                matched = [
                    (os.path.join(base_dir, e.name), e.name)
                    for e in entries
                    if not e.is_folder and fnmatch.fnmatch(e.name, maybe_pattern)
                ]
                return matched

        async def is_remote_folder(sc: StorageClient, remote_path: str) -> bool:
            """Return True if remote_path is a folder, otherwise False or it doesn't exist."""
            stat = await sc.stat(path=remote_path)
            if stat is None:
                return False
            else:
                return stat.is_folder

        async def remove_remote_path(sc: StorageClient, path: str, recursive: bool):
            """
            Removes a single remote path (file or folder). If it's a folder,
            recursively remove if `recursive` is True, otherwise fail.
            """
            # Does it exist at all?
            if not await sc.exists(path=path):
                print(
                    f"[yellow]Remote path '{path}' does not exist. Skipping.[/yellow]"
                )
                return

            # Check if it's a folder
            if await is_remote_folder(sc, path):
                # It's a folder
                if not recursive:
                    print(
                        f"[bold red]Cannot remove remote directory '{path}' without -r.[/bold red]"
                    )
                    raise typer.Exit(code=1)

                # Recursively remove contents
                entries = await sc.list(path=path)
                for e in entries:
                    child_path = os.path.join(path, e.name)
                    await remove_remote_path(sc, child_path, recursive)

                # Finally remove the folder itself (assuming storage.delete can remove empty folders)
                await sc.delete(path)
                print(
                    f"[bold cyan]Removed remote directory '{path}' recursively[/bold cyan]"
                )
            else:
                # It's a file
                await sc.delete(path)
                print(f"[bold cyan]Removed remote file '{path}'[/bold cyan]")

        # ----------------------------------------------------------------
        # 1) Expand the path if there's a wildcard
        # ----------------------------------------------------------------
        expanded_targets = []
        if scheme == "local":
            # Local expansions
            if any(c in subpath for c in ["*", "?", "["]):
                local_matches = local_list_files_with_glob(subpath)
                if not local_matches:
                    print(f"[bold red]No local files match '{subpath}'[/bold red]")
                    raise typer.Exit(code=1)
                # We'll store them (absolute_path, basename)
                expanded_targets = [(m, os.path.basename(m)) for m in local_matches]
            else:
                # Single path
                expanded_targets = [(subpath, os.path.basename(subpath))]
        else:
            # Remote expansions
            await ensure_storage_client()
            matches = await remote_list_files_with_glob(storage_client, subpath)
            if not matches:
                print(f"[bold red]No remote files/folders match '{subpath}'[/bold red]")
                raise typer.Exit(code=1)
            expanded_targets = matches

        # ----------------------------------------------------------------
        # 2) Perform the removal
        # ----------------------------------------------------------------
        for full_path, _ in expanded_targets:
            if scheme == "local":
                remove_local_path(full_path, recursive)
            else:
                await remove_remote_path(storage_client, full_path, recursive)
    finally:
        # Clean up remote client if used
        if client is not None:
            await client.__aexit__(None, None, None)
        if account_client is not None:
            await account_client.close()


@app.async_command("ls")
async def storage_ls_command(
    *,
    project_id: Annotated[
        Optional[str], typer.Option(..., help="Project ID (if remote)")
    ] = None,
    room: Annotated[
        Optional[str], typer.Option(..., help="Room name (if remote)")
    ] = None,
    token_path: Annotated[Optional[str], typer.Option()] = None,
    api_key_id: Annotated[
        Optional[str], typer.Option(..., help="API Key ID (if remote)")
    ] = None,
    name: Annotated[
        Optional[str], typer.Option(..., help="Participant name (if remote)")
    ] = None,
    role: str = "user",
    path: Annotated[
        str, typer.Argument(..., help="Path to list (local or room://...)")
    ],
    recursive: Annotated[
        bool, typer.Option("-r", help="List subfolders/files recursively")
    ] = False,
):
    """
    List files/folders either locally or in remote storage.
    - Supports wildcards on the path (e.g. *.txt).
    - Use -r for recursive listing.
    """

    scheme, subpath = parse_path(path)

    account_client = None
    client = None
    storage_client: Optional[StorageClient] = None

    # --- Set up remote connection if needed ---
    async def ensure_storage_client():
        nonlocal account_client, client, storage_client, project_id, api_key_id
        if storage_client is not None:
            return

        if not room:
            raise typer.BadParameter("To list a remote path, you must provide --room")

        account_client = await get_client()
        project_id = await resolve_project_id(project_id=project_id)
        api_key_id = await resolve_api_key(project_id, api_key_id)
        jwt = await resolve_token_jwt(
            project_id=project_id,
            api_key_id=api_key_id,
            token_path=token_path,
            name=name,
            role=role,
            room=room,
        )

        client = RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room, base_url=meshagent_base_url()),
                token=jwt,
            )
        )
        await client.__aenter__()
        storage_client = client.storage

    # ----------------------------------------------------------------
    # Local listing
    # ----------------------------------------------------------------
    def list_local_path(base_path: str, recursive: bool, prefix: str = ""):
        """
        List a local path. If it's a file, print it.
        If it's a directory, list its contents.
        If recursive=True, walk subdirectories as well.

        prefix is used to indent or prefix lines if desired.
        """

        if not os.path.exists(base_path):
            print(f"[red]{prefix}{base_path} does not exist[/red]")
            return

        if os.path.isfile(base_path):
            print(f"{prefix}{os.path.basename(base_path)}")
            return

        # If it's a directory
        # Print the directory name itself (if you want a heading)
        print(f"{prefix}{os.path.basename(base_path)}/")

        try:
            with os.scandir(base_path) as it:
                for entry in sorted(it, key=lambda x: x.name):
                    if entry.is_dir():
                        print(f"{prefix}  {entry.name}/")
                        if recursive:
                            # Recursively list the folder
                            list_local_path(
                                os.path.join(base_path, entry.name),
                                True,
                                prefix + "    ",
                            )
                    else:
                        print(f"{prefix}  {entry.name}")
        except PermissionError:
            print(f"{prefix}[PermissionError] Cannot list {base_path}")

    def glob_and_list_local(path_pattern: str, recursive: bool):
        """
        Glob for local paths. For each match:
          - If it's a file, print the file.
          - If it's a folder, list its contents.
        """
        matches = glob.glob(path_pattern)
        if not matches:
            print(f"[red]No local files/folders match '{path_pattern}'[/red]")
            return

        for m in matches:
            list_local_path(m, recursive)

    # ----------------------------------------------------------------
    # Remote listing
    # ----------------------------------------------------------------
    async def is_remote_folder(sc: StorageClient, remote_path: str) -> bool:
        """Return True if remote_path is a folder, otherwise False or it doesn't exist."""
        stat = await sc.stat(path=remote_path)

        if stat is None:
            return False
        else:
            return stat.is_folder

    async def list_remote_path(
        sc: StorageClient, remote_path: str, recursive: bool, prefix: str = ""
    ):
        """
        List a remote path. If it's a file, just print it.
        If it's a folder, list its contents. If recursive=True, list subfolders too.
        """

        # Does it exist at all?
        if not await sc.exists(path=remote_path):
            print(f"{prefix}[red]{remote_path} does not exist (remote)[/red]")
            return

        if await is_remote_folder(sc, remote_path):
            # It's a folder
            folder_name = os.path.basename(remote_path.rstrip("/")) or remote_path
            print(f"{prefix}{folder_name}/")
            if recursive:
                try:
                    entries = await sc.list(path=remote_path)
                    # Sort by name for consistent output
                    entries.sort(key=lambda e: e.name)
                    for e in entries:
                        child_path = os.path.join(remote_path, e.name)
                        if e.is_folder:
                            print(f"{prefix}  {e.name}/")
                            if recursive:
                                await list_remote_path(
                                    sc, child_path, recursive, prefix + "    "
                                )
                        else:
                            print(f"{prefix}  {e.name}")
                except Exception as ex:
                    print(
                        f"{prefix}[red]Cannot list remote folder '{remote_path}': {ex}[/red]"
                    )
        else:
            # It's a file
            print(f"{prefix}{os.path.basename(remote_path)}")

    async def glob_and_list_remote(
        sc: StorageClient, path_pattern: str, recursive: bool
    ):
        """
        If there's a wildcard, list matching files/folders. If no wildcard, list the single path.
        """
        base_dir, maybe_pattern = split_glob_subpath(path_pattern)
        if maybe_pattern is None:
            # Single path
            await list_remote_path(sc, base_dir, recursive)
        else:
            # Expand the directory
            # For 'ls' we might want to list matching files and also matching folders if pattern matches?
            # But your earlier approach for "cp" and "rm" only matched files in wildcard.
            # Let's do a slightly broader approach: match both files and folders by listing base_dir.
            if not await storage_client.exists(path=base_dir):
                print(f"[red]Remote folder '{base_dir}' does not exist[/red]")
                return
            try:
                entries = await sc.list(path=base_dir)
                # Filter by pattern
                matched = [e for e in entries if fnmatch.fnmatch(e.name, maybe_pattern)]
                if not matched:
                    print(f"[red]No remote entries match '{path_pattern}'[/red]")
                    return

                # For each match, build the full path and list it
                for e in matched:
                    child_path = os.path.join(base_dir, e.name)
                    await list_remote_path(sc, child_path, recursive)
            except Exception as ex:
                print(f"[red]Error listing remote '{base_dir}': {ex}[/red]")

    # ----------------------------------------------------------------
    # Execute listing based on local/remote
    # ----------------------------------------------------------------
    try:
        if scheme == "local":
            if any(c in subpath for c in ["*", "?", "["]):
                glob_and_list_local(subpath, recursive)
            else:
                # Single path (file or folder)
                if not os.path.exists(subpath):
                    print(f"[red]Local path '{subpath}' does not exist[/red]")
                    raise typer.Exit(code=1)
                list_local_path(subpath, recursive)
        else:
            await ensure_storage_client()
            # wildcard or single path
            if any(c in subpath for c in ["*", "?", "["]):
                await glob_and_list_remote(storage_client, subpath, recursive)
            else:
                # Single remote path
                await list_remote_path(storage_client, subpath, recursive)

    finally:
        if client is not None:
            await client.__aexit__(None, None, None)
        if account_client is not None:
            await account_client.close()
