import json
from pathlib import Path
import sys
import time
import unittest
from urllib.error import HTTPError

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/"src"),str(ROOT/"tools")]
from message_queue.common import rpc
from lab import Cluster


class ClusterTests(unittest.TestCase):
    def setUp(self):
        self.cluster=Cluster(lease=5).start()
        self.client=self.cluster.client

    def tearDown(self):
        self.cluster.close()

    def error(self,status,path,**body):
        with self.assertRaises(HTTPError) as caught:
            self.client.call(path,**body)
        self.assertEqual(caught.exception.code,status)

    def source(self,member,partition=0):
        return {"topic":"input","group":"workers","member":member["member"],"session":member["session"],"generation":member["generation"],"partition":partition}

    def test_quorum_retry_leader_crash_restart_and_producer_fence(self):
        self.client.call("/topic",topic="input",partitions=2)
        producer=self.client.call("/producer",producer="sender")
        request={"topic":"input","partition":0,**producer,"sequence":0,"messages":[{"value":"first","key":"k"},{"value":"second"}]}
        result=self.client.call("/produce",**request)
        self.assertTrue(result["committed"])
        self.assertEqual(self.client.call("/produce",**request),result)  # Deliberately discard/retry first ACK.
        self.error(409,"/produce",**{**request,"messages":[{"value":"changed"}]})
        self.cluster.stop("b0")
        fetched=self.client.call("/fetch",topic="input",partition=0)
        self.assertEqual([r["value"] for r in fetched["records"]],["first","second"])
        self.assertNotEqual(fetched["leader"],"b0")
        self.assertGreater(fetched["epoch"],0)
        self.client.call("/produce",**{**request,"sequence":1,"messages":[{"value":"third"}]})
        self.cluster.start_broker(0)
        self.client.call("/produce",**{**request,"sequence":2,"messages":[{"value":"fourth"}]})
        self.cluster.stop("coordinator")
        self.cluster.start_coordinator()
        self.assertEqual(len(self.client.call("/fetch",topic="input",partition=0)["records"]),4)
        self.assertEqual(self.client.call("/produce",**request),result)
        self.client.call("/producer",producer="sender")
        self.error(409,"/produce",**{**request,"sequence":3})

    def test_transport_partition_provisional_ack_and_quorum_recovery(self):
        self.client.call("/topic",topic="input")
        producer=self.client.call("/producer",producer="sender")
        self.cluster.partition(1)
        self.cluster.partition(2)
        # Brokers remain alive: the network proxies, not the processes, are cut.
        for i in (1,2):
            self.assertEqual(rpc(f"http://127.0.0.1:{self.cluster.backend_ports[i]}","/health",{},self.cluster.config["cluster_token"])["role"],"broker")
        body={"topic":"input","partition":0,**producer,"sequence":0,"messages":[{"value":"pending"}]}
        self.error(503,"/produce",**body)
        self.assertEqual(self.client.call("/fetch",topic="input",partition=0)["records"],[])
        provisional=self.client.call("/produce",**body,acks="leader")
        self.assertFalse(provisional["committed"])
        self.cluster.partition(1,False)
        committed=self.client.call("/produce",**body)
        self.assertEqual(committed["offsets"],[0])
        self.assertEqual(len(self.client.call("/fetch",topic="input",partition=0)["records"]),1)

    def test_group_rebalance_delivery_semantics_and_session_fencing(self):
        self.client.call("/topic",topic="input",partitions=2)
        producer=self.client.producer("p")
        producer.send("input",[{"value":"x"}],partition=0)
        first=self.client.call("/join",topic="input",group="workers",member="a")
        source=self.source(first)
        self.assertEqual(self.client.call("/group-fetch",**source)["next_offset"],1)
        self.assertEqual(len(self.client.call("/group-fetch",**source)["records"]),1)
        self.error(409,"/commit",**source,next_offset=2)
        second=self.client.call("/join",topic="input",group="workers",member="b")
        self.error(409,"/commit",**source,next_offset=1)
        refreshed=self.client.call("/heartbeat",topic="input",group="workers",member="a",session=first["session"])
        source=self.source(refreshed)
        self.assertEqual(refreshed["assignments"],[0])
        self.client.call("/group-fetch",**source,semantics="at_most_once")
        self.assertEqual(self.client.call("/group-fetch",**source)["records"],[])
        replacement=self.client.call("/join",topic="input",group="workers",member="a")
        self.error(409,"/heartbeat",topic="input",group="workers",member="a",session=first["session"])
        self.assertNotEqual(replacement["session"],first["session"])

    def test_atomic_process_retry_dead_letter_delay_and_archive(self):
        for topic in ("input","output","retry","dead","replay"):
            self.client.call("/topic",topic=topic)
        self.client.producer("p").send("input",[{"value":"work"}])
        member=self.client.call("/join",topic="input",group="workers",member="a")
        source=self.source(member)
        record=self.client.call("/group-fetch",**source)["records"][0]
        body={"txid":"tx1","source":source,"expected_offset":0,"next_offset":1,"outputs":[{"topic":"output","messages":[{"value":"processed"}]}]}
        first=self.client.call("/process",**body)
        self.assertEqual(self.client.call("/process",**body),first)
        self.assertEqual(len(self.client.call("/fetch",topic="output",partition=0)["records"]),1)
        self.assertEqual(self.client.call("/group-fetch",**source)["records"],[])
        self.client.call("/reset",**source,offset="earliest")
        self.client.call("/group-fetch",**source)
        self.client.retry("retry1",source,0,1,record,"retry","dead",delay_seconds=0.2)
        self.cluster.wait(lambda:self.client.call("/fetch",topic="retry",partition=0)["records"])
        self.client.call("/reset",**source,offset="earliest")
        self.client.call("/group-fetch",**source)
        self.client.retry("dead1",source,0,1,record,"retry","dead",attempt=3,max_attempts=3)
        self.assertEqual(len(self.client.call("/fetch",topic="dead",partition=0)["records"]),1)
        archive=self.cluster.root/"archive.json"
        self.assertEqual(self.client.archive("output",0,archive),{"records":1})
        self.client.replay(archive,self.client.producer("replayer"),"replay")
        self.assertEqual(self.client.call("/fetch",topic="replay",partition=0)["records"][0]["value"],"processed")

    def test_disk_corruption_excludes_replica_and_preserves_committed_records(self):
        self.client.call("/topic",topic="input",segment_bytes=512)
        self.client.producer("p").send("input",[{"value":"safe"}])
        self.cluster.stop("b0")
        paths=list((self.cluster.root/"b0/input/0").glob("generation-*/*.log"))
        with paths[-1].open("r+b") as output:
            output.seek(-1,2)
            old=output.read(1)
            output.seek(-1,2)
            output.write(bytes([old[0]^1]))
        self.cluster.start_broker(0)
        fetched=self.client.call("/fetch",topic="input",partition=0)
        self.assertEqual(fetched["records"][0]["value"],"safe")
        self.assertNotEqual(fetched["leader"],"b0")


if __name__=="__main__":
    unittest.main()
