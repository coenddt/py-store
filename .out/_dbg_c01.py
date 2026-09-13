import asyncio, os, sys
sys.path.insert(0, os.path.expandvars(r"%CD%\..\src"))
sys.path.insert(0, r"f:\独立开发者\项目\mongo-store\py-store\src")
sys.path.insert(0, r"f:\独立开发者\项目\mongo-store\py-store\example\course-platform\impl")
import harness, json

async def main():
    res = await harness.run_backend('mongodb')
    for r in res['results']:
        if r['id'] in ('C-01','C-03','C-07'):
            print('====', r['id'], r['status'])
            for s in r['steps']:
                print('  step', s['idx'], s['op'], 'ok=', s['ok'])
                if not s['ok']:
                    print('   note:', s['note'][:500])

asyncio.run(main())