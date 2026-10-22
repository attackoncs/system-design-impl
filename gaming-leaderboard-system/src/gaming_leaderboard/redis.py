"""Small RESP2 client; always talks to an actual Redis server."""
import socket


class RedisError(Exception):
    pass


class Redis:
    def __init__(self,host,port,password):
        self.host,self.port,self.password=host,port,password

    @staticmethod
    def encode(args):
        chunks=[b"*"+str(len(args)).encode()+b"\r\n"]
        for arg in args:
            data=arg if isinstance(arg,bytes) else str(arg).encode()
            chunks.extend([b"$"+str(len(data)).encode()+b"\r\n",data,b"\r\n"])
        return b"".join(chunks)

    @classmethod
    def read(cls,stream):
        line=stream.readline(1024*1024)
        if not line or not line.endswith(b"\r\n"):
            raise RedisError("truncated RESP response")
        marker,value=line[:1],line[1:-2]
        if marker==b"+":
            return value.decode()
        if marker==b"-":
            raise RedisError(value.decode(errors="replace"))
        if marker==b":":
            return int(value)
        length=int(value)
        if length==-1:
            return None
        if length<0 or length>8*1024*1024:
            raise RedisError("RESP response exceeds budget")
        if marker==b"$":
            data=stream.read(length+2)
            if len(data)!=length+2 or not data.endswith(b"\r\n"):
                raise RedisError("truncated RESP bulk")
            return data[:-2].decode()
        if marker==b"*":
            return [cls.read(stream) for _ in range(length)]
        raise RedisError("unsupported RESP type")

    def command(self,*args,timeout=3):
        with socket.create_connection((self.host,self.port),timeout=min(.3,timeout)) as connection:
            connection.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
            connection.settimeout(timeout)
            with connection.makefile("rb") as stream:
                connection.sendall(self.encode(["AUTH",self.password]))
                self.read(stream)
                connection.sendall(self.encode(args))
                return self.read(stream)
