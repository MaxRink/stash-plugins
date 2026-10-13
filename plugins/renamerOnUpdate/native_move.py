"""Small adapter for Stash's native moveFiles mutation."""


def move_file(call_graphql, file_id: str, destination_folder: str, destination_basename: str) -> None:
    query = """
    mutation MoveFiles($input: MoveFilesInput!) {
        moveFiles(input: $input)
    }
    """
    variables = {"input": {
        "ids": [str(file_id)],
        "destination_folder": destination_folder,
        "destination_basename": destination_basename,
    }}
    result = call_graphql(query, variables)
    if not result or result.get("moveFiles") is not True:
        raise RuntimeError(f"native moveFiles rejected file {file_id}")
