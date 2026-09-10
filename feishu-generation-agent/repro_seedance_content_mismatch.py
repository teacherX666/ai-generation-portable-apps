
import asyncio, hashlib, json, urllib.request
from pathlib import Path
from feishu_generation_agent.domain.plan import GenerationTask
from feishu_generation_agent.domain.document import MediaAsset
from feishu_generation_agent.integrations.seedance import SeedanceVideoGenerator
import httpx
RUN = "c1f3a085-7252-4e5e-90ea-93bf4602776c"
req=urllib.request.Request(f"http://127.0.0.1:8765/api/runs/{RUN}",headers={"X-Portal-User-Id":"6b34d9e8-3058-41b6-89b2-4cedac2362ee"})
with urllib.request.urlopen(req, timeout=20) as r: view=json.load(r)
task=GenerationTask.model_validate(view["approval"]["tasks"][0])
root=Path("data/runs/ZI24dwvV4o16QoxEWElcKHbnnvd/inputs")
paths={
 "image-1": root/"b3897459617f7156996496329ea0c3298c1fcdb020f41c5fcaa9bec52907924e.png",
 "image-2": root/"8401af8237ff23f033b1a5b43dcce75b0d3445f7eac0a5681554c0b61799fac6.jpg",
}
assets=[]
for raw in view["approval"]["media_assets"]:
    item=dict(raw); path=paths[item["asset_id"]]; data=path.read_bytes()
    item["local_path"]=str(path); item["sha256"]=hashlib.sha256(data).hexdigest(); item["size"]=len(data); item.setdefault("source_block_id", item["asset_id"]); item.setdefault("origin", "feishu"); item.setdefault("mime_type", "image/png" if item["asset_id"]=="image-1" else "image/jpeg")
    assets.append(MediaAsset.model_validate(item))
print("TASK_REFS", [(r.asset_id,r.role,r.order) for r in task.reference_images])
print("ASSETS", [(a.asset_id,a.size,a.sha256,a.local_path) for a in assets])
async def main():
    async with httpx.AsyncClient(trust_env=False) as client:
        generator=SeedanceVideoGenerator(client,base_url="https://ark.cn-beijing.volces.com/api/v3",api_key="dummy",model="dummy")
        try:
            import os
            for a in assets:
                st=a.local_path.lstat(); print("STAT",a.asset_id,st.st_size,st.st_mtime_ns,a.size,a.sha256)
                fd=os.open(a.local_path, os.O_RDONLY); fs=os.fstat(fd); data=os.read(fd, 7000000); os.close(fd); print("READ",a.asset_id,fs.st_size,len(data),hashlib.sha256(data).hexdigest())
            for a in assets:
                st=a.local_path.lstat()
                try:
                    data=generator._read_verified_asset(a, st)
                    print("READ_VERIFIED",a.asset_id,len(data),hashlib.sha256(data).hexdigest())
                except Exception as ex:
                    print("READ_VERIFIED_FAILED",a.asset_id,type(ex).__name__,getattr(getattr(ex,"detail",None),"model_dump",lambda: str(ex))())
            refs, ordered, contents=generator._validate_submission(task, assets)
            print("VALIDATED", [(a.asset_id,len(c)) for a,c in zip(ordered,contents,strict=True)])
        except Exception as exc:
            print("FAILED", type(exc).__name__, str(exc))
            detail=getattr(exc,"detail",None)
            if detail is not None: print("DETAIL", detail.model_dump())
asyncio.run(main())
