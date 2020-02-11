Mostly based on the sendrecv demo in https://github.com/centricular/gstwebrtc-demos/

## Dependencies

* Python 3
* python-websockets
* python-asyncio
* python-aiohttp
* python-gstreamer
* gstreamer bad plugins

## Example usage

In three separate tabs, run consecutively:

```console
$ ./generate_cert.sh
$ ./control-server.py
$ ./https-server.py --cert-file cert.pem --key-file key.pem --port 8000
```
