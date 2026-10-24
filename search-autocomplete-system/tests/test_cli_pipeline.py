"""Exercise the documented collection/aggregation/build/cleanup command path."""

import json

from search_autocomplete.cli import main


def test_cli_pipeline(tmp_path, capsys):
    database = str(tmp_path / "cli.sqlite3")

    def command(*args):
        main(["--db", database, *args])
        return json.loads(capsys.readouterr().out)

    assert command("record", "a", "apple", "--at", "1") == {
        "selected": True,
        "created": True,
    }
    aggregation = command("aggregate", "--start", "0", "--end", "2")
    materialized = command("aggregation", str(aggregation["aggregation_id"]))
    assert materialized["frequencies"] == [["apple", 1]]
    command("build", "--start", "0", "--end", "2")
    assert command("query", "a") == [["apple", 1]]
    command("block", "apple")
    assert command("purge-blocked") == {"deleted": 1}
    command("unblock", "apple")
    command("worker", "--once", "--purge-blocked")
    assert command("query", "a") == []
