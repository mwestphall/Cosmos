import requests
import glob
import uuid
import os
import argparse
from concurrent.futures import ThreadPoolExecutor
import base64
import logging
import time
from tqdm import tqdm

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()
logger.setLevel(logging.INFO)

def run_request(payload):
    filename, dataset_id = payload
    with open(filename, 'rb') as rf:
        bstring = base64.b64encode(rf.read()).decode()

        result = requests.post('http://ingestion:8000/preprocess', json={'pdf': bstring, 'dataset_id': dataset_id, 'pdf_name': os.path.basename(filename)})
    return result


def run(directory, bsz):
    did = str(uuid.uuid4())
    logger.info(f'Dataset id generated: {did}')
    filenames = glob.glob(os.path.join(directory, '*.pdf'))
    if len(filenames) == 0:
        logger.error('Empty input directory')
        return None, None
    fsizes = [os.stat(f).st_size for f in filenames]
    fz = zip(filenames, fsizes)
    srt = sorted(fz, key=lambda x: x[1], reverse=True)
    filenames, _ = zip(*srt)
    dids = [did] * len(filenames)
    zipped = list(zip(filenames, dids))
    logger.info('Submitting jobs')
    with ThreadPoolExecutor(max_workers=32) as pool:
        resps = list(tqdm(pool.map(run_request, zipped), total=len(zipped)))
    logger.info('Finished submitting jobs')
    successful_resps = [r for r in resps if r.status_code == 200]
    logger.info(f'There were {len(resps) - len(successful_resps)} failed job submissions')
    pages_dict = {}
    with tqdm(total=len(successful_resps)) as pbar:
        done_count = 0
        error_count = 0
        prev_done_count = 0
        while done_count < len(successful_resps):
            done_count = 0
            error_count = 0
            for resp in successful_resps:
                obj = resp.json()
                tid = obj['data']['task_id']
                url = f'http://ingestion:8000/status/{tid}'
                result = requests.get(url)
                if result.status_code == 200:
                    obj = result.json()
                    status = obj['status']
                    if status == 'SUCCESS':
                        done_count += 1
                    elif status == 'FAILURE':
                        done_count += 1
                        error_count += 1
                else:
                    error_count += 1
                    done_count += 1
            if prev_done_count < done_count:
                pbar.update(done_count - prev_done_count)
                prev_done_count = done_count
            time.sleep(10)
    logger.info(f'Done ingesting. There were {error_count} failures')
    #tids = []
    #for resp in successful_resps:
    #    obj = resp.json()
    #    tid = obj['data']['task_id']
    #    url = f'http://ingestion:8000/status/{tid}'
    #    result = requests.get(url)
    #    if result.status_code == 200:
    #        obj = result.json()
    #        status = obj['status']
    #        if status == 'SUCCESS':
    #            task_result = obj['result']
    #            page_task_ids = task_result['page_tasks']
    #            tids.extend(page_task_ids)
    #logger.info(f'Now monitoring page level jobs')
    #with tqdm(total=len(tids)) as pbar:
    #    done_count = 0
    #    prev_done_count = 0
    #    while done_count < len(tids):
    #        done_count = 0
    #        error_count = 0
    #        for tid in tids:
    #            url = f'http://ingestion:8000/status/{tid}'
    #            result = requests.get(url)
    #            if result.status_code == 200:
    #                obj = result.json()
    #                status = obj['status']
    #                if status == 'SUCCESS':
    #                    done_count += 1
    #                elif status == 'FAILURE':
    #                    done_count += 1
    #                    error_count += 1
    #            else:
    #                error_count += 1
    #                done_count += 1
    #        if prev_done_count < done_count:
    #            pbar.update(done_count - prev_done_count)
    #            prev_done_count = done_count
    #        time.sleep(10)
    #logger.info('Done processing all pages')


def delete(did):
    result = requests.post('http://ingestion:8000/delete', json={'dataset_id':did})
    logger.info(result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', help='Path to pdf directory')
    args = parser.parse_args()
    bsz = int(os.environ['BSZ'])
    stime = time.time()
    run(args.directory, bsz)
    time_up = time.time() - stime
    logger.info(f'TOTAL TIME UP: {time_up} seconds')

    
