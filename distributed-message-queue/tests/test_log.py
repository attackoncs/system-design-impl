from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from message_queue.common import Error,ZERO,envelope
from message_queue.log import Corruption,PartitionLog


def records(count,start=0,previous=ZERO):
    result=[]
    for i in range(start,start+count):
        value=envelope({"offset":i,"timestamp":float(i),"value":"x"*80},previous)
        result.append(value)
        previous=value["hash"]
    return result


class LogTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)/"partition"
        self.log=PartitionLog(self.root,segment_bytes=512)

    def tearDown(self):
        self.temp.cleanup()

    def test_segment_rotation_index_rebuild_and_restart(self):
        values=records(10)
        self.log.append(values)
        self.assertGreater(len(list(self.log.directory.glob("*.log"))),1)
        for path in self.log.directory.glob("*.idx"):
            path.unlink()
        reopened=PartitionLog(self.root,512)
        self.assertEqual(reopened.read(0),values)
        self.assertTrue(list(reopened.directory.glob("*.idx")))

    def test_torn_active_tail_is_recovered(self):
        self.log.append(records(3))
        active=sorted(self.log.directory.glob("*.log"))[-1]
        with active.open("ab") as output:
            output.write(b"MQ")
        self.assertEqual(PartitionLog(self.root,512).next,3)

    def test_complete_corruption_is_not_silently_truncated(self):
        self.log.append(records(3))
        active=sorted(self.log.directory.glob("*.log"))[-1]
        with active.open("r+b") as output:
            output.seek(-1,2)
            old=output.read(1)
            output.seek(-1,2)
            output.write(bytes([old[0]^1]))
        with self.assertRaises(Corruption):
            self.log.read(2)
        with self.assertRaises(Corruption):
            PartitionLog(self.root,512)

    def test_fencing_truncates_only_uncommitted_suffix(self):
        values=records(5)
        self.log.append(values)
        self.log.align(2,2,values[2]["hash"])
        self.assertEqual(self.log.next,3)
        with self.assertRaises(Error):
            self.log.align(1,2,values[2]["hash"])
        with self.assertRaises(Error):
            self.log.align(3,2,ZERO)
        self.log.append(records(1,start=3,previous=values[2]["hash"]))
        self.assertEqual(self.log.next,4)

    def test_retention_floor_chain_checkpoint_and_snapshot(self):
        values=records(8)
        self.log.append(values)
        original=len(list(self.log.directory.glob("*.log")))
        self.log.retain(6,values[5]["hash"])
        self.assertLess(len(list(self.log.directory.glob("*.log"))),original)
        with self.assertRaises(Error) as caught:
            self.log.read(0)
        self.assertEqual(caught.exception.status,410)
        self.assertEqual(PartitionLog(self.root,512).read(6),values[6:])
        target=PartitionLog(Path(self.temp.name)/"replica",512)
        target.install(values[6:],6,values[5]["hash"],10)
        self.assertEqual(target.read(6),values[6:])
        with self.assertRaises(Error):
            target.install(values[6:],6,values[5]["hash"],9)


if __name__=="__main__":
    unittest.main()
