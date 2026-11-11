"""Keep Docker stdout/stderr bounded before it enters Python or an RPC response."""
import subprocess


def capture(container_id):
    process=subprocess.Popen(['docker','logs','--timestamps','--tail','200',container_id],stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    try:
        result=subprocess.run(['tail','-c','65536'],stdin=process.stdout,capture_output=True,check=True,timeout=30)
        process.stdout.close()
        if process.wait(timeout=5):raise RuntimeError('Runtime log collection failed')
        return result.stdout.decode(errors='replace')
    finally:
        process.stdout.close()
        if process.poll() is None:process.kill();process.wait()
