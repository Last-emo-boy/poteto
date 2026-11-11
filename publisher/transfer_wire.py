"""Bounds shared by the standard-library client and project bundle reader."""
import os

FORMAT='ovh-paas-project/1'
BUNDLE_LIMIT=14*1024**3
CHUNK=8*1024**2


class BundleError(ValueError):
    pass


class BoundedZipInput:
    """Limit ZIP central-directory reads before zipfile allocates contents."""
    def __init__(self,source):self.source=source;self.size=os.fstat(source.fileno()).st_size
    def seek(self,*args):return self.source.seek(*args)
    def tell(self):return self.source.tell()
    def seekable(self):return True
    def read(self,size=-1):
        if size<0:size=max(0,self.size-self.tell())
        if size>CHUNK:raise BundleError('ZIP metadata or read exceeds 8 MiB')
        return self.source.read(size)
