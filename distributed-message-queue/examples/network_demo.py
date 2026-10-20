import json
from pathlib import Path
import sys
from urllib.error import HTTPError

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools"))
from lab import Cluster


def main():
    with Cluster() as cluster:
        client=cluster.client
        for topic in ("input","output"):
            client.call("/topic",topic=topic)
        producer=client.call("/producer",producer="demo")
        body={"topic":"input","partition":0,**producer,"sequence":0,"messages":[{"value":"one"},{"value":"two"}]}
        first=client.call("/produce",**body)
        duplicate=client.call("/produce",**body)
        cluster.stop("b0")
        after=client.call("/fetch",topic="input",partition=0)
        cluster.partition(2)
        request={**body,"sequence":1,"messages":[{"value":"three"}]}
        failure=None
        try:
            client.call("/produce",**request)
        except HTTPError as exc:
            failure=exc.code
        cluster.partition(2,False)
        recovered=client.call("/produce",**request)
        member=client.call("/join",topic="input",group="demo",member="worker")
        source={"topic":"input","group":"demo","member":"worker","session":member["session"],"generation":member["generation"],"partition":0}
        fetched=client.call("/group-fetch",**source)
        transaction={"txid":"demo_tx","source":source,"expected_offset":0,"next_offset":fetched["next_offset"],"outputs":[{"topic":"output","messages":[{"value":"processed three input messages"}]}]}
        processed=client.call("/process",**transaction)
        assert client.call("/process",**transaction)==processed
        print(json.dumps({"broker_processes":3,"coordinator_processes":1,"tcp_fault_proxy_processes":3,"independent_broker_disks":3,"initial_offsets":first["offsets"],"identical_retry_same_result":first==duplicate,"leader_after_crash":after["leader"],"leader_epoch":after["epoch"],"minority_write_status":failure,"recovered_offset":recovered["offsets"],"transaction_input_next":processed["next_offset"],"transaction_output_count":len(client.call("/fetch",topic="output",partition=0)["records"])},indent=2))


if __name__=="__main__":
    main()
