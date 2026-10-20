import json
from pathlib import Path
import sys
import time
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request,urlopen
from xml.etree import ElementTree

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/"src"),str(ROOT/"tools")]
from metrics_monitor.common import http_json
from lab import Lab


class NetworkTests(unittest.TestCase):
    def test_real_pull_push_replay_discovery_failure_and_chart(self):
        with Lab() as lab:
            lab.wait(lambda:lab.points("python_memory_bytes"))
            lab.wait(lambda:lab.points("process_cpu_seconds_total",aggregation="last"))
            lab.wait(lambda:lab.points("http_requests_total",aggregation="last"))
            with self.assertRaises(HTTPError) as caught:
                http_json(lab.urls["collector"]+"/push","POST",{"samples":[]})
            self.assertEqual(caught.exception.code,401)
            lab.stop("storage")
            now=time.time()
            body=[{"metric":"replay_test","type":"gauge","labels":{"instance":"one"},"timestamp":now,"value":7}]
            first=lab.push(body,"stable-push")
            self.assertEqual(lab.push(body,"stable-push"),first)
            lab.wait(lambda:http_json(lab.urls["collector"]+"/health")["pending_chunks"]==0)
            self.assertEqual(lab.points("replay_test"),[])
            lab.start_role("storage")
            lab.wait(lambda:lab.points("replay_test"))
            self.assertEqual(sum(p["count"] for p in lab.points("replay_test")),1)
            lab.stop("exporter")
            lab.wait(lambda:any(p["value"]==0 for p in lab.points("collector_up",aggregation="last",step=1)))
            params=urlencode({"metric":"replay_test","start":now-1,"end":now+1,"step":1})
            with urlopen(Request(lab.urls["api"]+"/chart.svg?"+params,headers={"Authorization":"Bearer "+lab.config["read_token"]})) as response:
                svg=response.read()
            self.assertEqual(ElementTree.fromstring(svg).tag,"{http://www.w3.org/2000/svg}svg")
            with urlopen(lab.urls["api"]+"/dashboard") as response:
                self.assertIn(b"Infrastructure Metrics",response.read())

    def test_real_webhook_lost_confirmation_dedup_and_resolution(self):
        with Lab() as lab:
            rule={"id":"hot","metric":"temperature","op":">","threshold":80,"for_seconds":0,"destination":"local","group_by":["instance"]}
            http_json(lab.urls["api"]+"/rules","POST",rule,lab.config["admin_token"])
            lab.receiver_control.write_text('{"fail_after_commit":true}')
            now=time.time()
            lab.push([{"metric":"temperature","timestamp":now,"value":90,"labels":{"instance":"host","core":str(i)}} for i in range(2)],"hot-batch")
            lab.wait(lambda:http_json(lab.urls["receiver"]+"/stats")["unique_notifications"]==1)
            initial=http_json(lab.urls["receiver"]+"/stats")
            self.assertEqual(len(initial["receipts"][0]["payload"]["events"]),2)
            lab.receiver_control.write_text('{"fail_after_commit":false}')
            lab.wait(lambda:http_json(lab.urls["receiver"]+"/stats")["attempts"]>=2)
            self.assertEqual(http_json(lab.urls["receiver"]+"/stats")["unique_notifications"],1)
            lab.wait(lambda:any(row["status"]=="sent" for row in http_json(lab.urls["api"]+"/alerts",token=lab.config["read_token"])["deliveries"]))
            lab.push([{"metric":"temperature","timestamp":time.time(),"value":10,"labels":{"instance":"host","core":str(i)}} for i in range(2)],"cool-batch")
            lab.wait(lambda:http_json(lab.urls["receiver"]+"/stats")["unique_notifications"]==2)
            final=http_json(lab.urls["receiver"]+"/stats")
            self.assertTrue(all(event["state"]=="resolved" for event in final["receipts"][-1]["payload"]["events"]))


if __name__=="__main__":
    unittest.main()
