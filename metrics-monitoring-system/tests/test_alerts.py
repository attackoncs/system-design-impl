from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from metrics_monitor.alerts import Alerts,Provider,WebhookProvider
from metrics_monitor.common import Error
from metrics_monitor.pipeline import Collector
from metrics_monitor.storage import Store


class RecordingProvider(Provider):
    def __init__(self,fail=0):
        self.fail,self.calls,self.received=fail,[],{}

    def deliver(self,destination,channel,payload,key):
        self.calls.append(key)
        self.received[key]=payload
        if self.fail:
            self.fail-=1
            raise OSError("injected lost confirmation after receipt")


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.now=[2000001600.0]
        self.store=Store(Path(self.temp.name)/"metrics.sqlite",clock=lambda:self.now[0])
        self.alerts=Alerts(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def ingest(self,value,cores=1):
        self.store.ingest("b"+str(self.now[0])+str(value),[{"metric":"cpu","type":"gauge","labels":{"instance":"host","core":str(i)},"timestamp":self.now[0],"value":value} for i in range(cores)])

    def rule(self,duration=0):
        return self.alerts.put_rule({"id":"hot","metric":"cpu","op":">","threshold":80,"for_seconds":duration,"destination":"test","group_by":["instance"]})

    def test_pending_flapping_firing_no_data_and_resolution(self):
        self.rule(60)
        self.ingest(90)
        self.assertEqual(self.alerts.evaluate()["alerts"][0]["state"],"pending")
        self.now[0]+=30
        self.ingest(10)
        self.assertEqual(self.alerts.evaluate()["alerts"][0]["state"],"inactive")
        self.now[0]+=1
        self.ingest(90)
        self.alerts.evaluate()
        self.now[0]+=60
        self.assertEqual(self.alerts.evaluate()["alerts"][0]["state"],"firing")
        self.now[0]+=301
        alert=self.alerts.evaluate()["alerts"][0]
        self.assertEqual(alert["state"],"firing")
        self.assertEqual(alert["no_data"],1)
        self.ingest(1)
        self.assertEqual(self.alerts.evaluate()["alerts"][0]["state"],"resolved")

    def test_grouping_durable_retry_keys_and_restart(self):
        self.rule()
        self.ingest(90,cores=2)
        self.alerts.evaluate()
        provider=RecordingProvider(fail=1)
        self.assertTrue(self.alerts.notify_once(provider))
        self.assertEqual(len(provider.received),1)
        self.assertEqual(len(next(iter(provider.received.values()))["events"]),2)
        self.now[0]+=2
        alerts=Alerts(Store(self.store.path,clock=lambda:self.now[0]))
        self.assertTrue(alerts.notify_once(provider))
        self.assertEqual(provider.calls[0],provider.calls[1])
        self.assertEqual(alerts.status()["deliveries"][0]["status"],"sent")
        self.assertFalse(alerts.notify_once(provider))

    def test_advancing_clock_does_not_invent_a_silence(self):
        def ticking():
            self.now[0]+=0.001
            return self.now[0]
        self.store.clock=ticking
        self.rule()
        self.ingest(90)
        self.alerts.evaluate()
        provider=RecordingProvider()
        self.assertTrue(self.alerts.notify_once(provider))
        self.assertEqual(len(provider.received),1)

    def test_silence_and_superseded_pending_transition(self):
        self.rule()
        self.alerts.silence({"instance":"host"},self.now[0],self.now[0]+10)
        self.ingest(90)
        self.alerts.evaluate()
        provider=RecordingProvider()
        self.assertFalse(self.alerts.notify_once(provider))
        self.now[0]+=5
        self.ingest(1)
        self.alerts.evaluate()
        self.now[0]+=5
        self.assertTrue(self.alerts.notify_once(provider))
        states=[event["state"] for event in next(iter(provider.received.values()))["events"]]
        self.assertEqual(states,["resolved"])

    def test_unconfigured_channel_backoff_and_dead_letter(self):
        rule={**self.rule(),"id":"mail","channel":"email"}
        self.alerts.put_rule(rule)
        self.ingest(90)
        self.alerts.evaluate()
        provider=WebhookProvider({})
        for i in range(5):
            # Two groups, each has a stable delivery and independent retries.
            self.alerts.notify_once(provider)
            self.alerts.notify_once(provider)
            self.now[0]+=2**(i+1)
        self.assertTrue(all(row["status"]=="dead" for row in self.alerts.status()["deliveries"]))
        with self.assertRaises(Error):
            self.alerts.put_rule({**rule,"threshold":70})

    def test_collector_backpressure_replay_and_fifo_retry(self):
        collector=Collector(Path(self.temp.name)/"spool.sqlite",max_bytes=500,clock=lambda:self.now[0])
        body=[{"metric":"cpu","timestamp":self.now[0],"value":1}]
        first=collector.enqueue(body,"a")
        self.assertEqual(collector.enqueue(body,"a"),first)
        with self.assertRaises(Error):
            collector.enqueue([{**body[0],"value":2}],"a")
        with self.assertRaises(Error) as caught:
            collector.enqueue(body*10,"too_big")
        self.assertEqual(caught.exception.status,429)
        class Queue:
            def __init__(self):
                self.fail=True
                self.messages=[]
            def publish(self,payload):
                self.messages.append(payload)
                if self.fail:
                    raise OSError("injected queue outage")
        queue=Queue()
        self.assertFalse(collector.flush_once(queue))
        self.now[0]+=1
        queue.fail=False
        self.assertTrue(collector.flush_once(queue))
        self.assertEqual(queue.messages[0],queue.messages[1])
        self.assertEqual(collector.health()["pending_bytes"],0)


if __name__=="__main__":
    unittest.main()
