# Changelog

## [0.13.0](https://github.com/rtammekivi/hikconnect_intercom/compare/v0.12.0...v0.13.0) (2026-10-26)


### Features

* **event:** include caller details in doorbell ring events ([#30](https://github.com/rtammekivi/hikconnect_intercom/issues/30)) ([efb1a4b](https://github.com/rtammekivi/hikconnect_intercom/commit/efb1a4b7f623c2ac73c7ba1558e130bc3e3f02dd))

## [0.12.0](https://github.com/rtammekivi/hikconnect_intercom/compare/v0.11.2...v0.12.0) (2026-10-25)


### Features

* **config:** make the call-status poll interval configurable ([#31](https://github.com/rtammekivi/hikconnect_intercom/issues/31)) ([d640a2c](https://github.com/rtammekivi/hikconnect_intercom/commit/d640a2c438fea541a50a387fb5fab218797ce413))

## [0.11.2](https://github.com/rtammekivi/hikconnect_intercom/compare/v0.11.1...v0.11.2) (2026-10-19)


### Bug Fixes

* **cas:** normalize CAS port to integer for Russian endpoint ([#26](https://github.com/rtammekivi/hikconnect_intercom/issues/26)) ([5a5ad02](https://github.com/rtammekivi/hikconnect_intercom/commit/5a5ad02092dca160ebb225e4037efe2090a89721))

## [0.11.1](https://github.com/rtammekivi/hikconnect_intercom/compare/v0.11.0...v0.11.1) (2026-10-19)


### Bug Fixes

* **cas:** handle Russian CAS hostname mismatch with certificate pinning ([fff4872](https://github.com/rtammekivi/hikconnect_intercom/commit/fff48728379e8bc7c1cea849fd589b6f793a12de))
* **cas:** pin legacy Russian CAS certificate for hostname mismatch ([7a135d5](https://github.com/rtammekivi/hikconnect_intercom/commit/7a135d5c6bb6a13867f5bb224bf3874c95ebd183))

## [0.11.0](https://github.com/rtammekivi/hikconnect_intercom/compare/v0.10.8...v0.11.0) (2026-10-14)


### Features

* doorbell event, last ring and station status sensors ([3f92eb4](https://github.com/rtammekivi/hikconnect_intercom/commit/3f92eb430ff71c79c0c2a7d8c34742de39ff1c56))


### Bug Fixes

* always write doorbell entity state on coordinator updates ([4d08ddb](https://github.com/rtammekivi/hikconnect_intercom/commit/4d08ddb163cd427c740e9545a878148aba3a66c1))
* call controls, push and ring history parity with the Hik-Connect app ([ab32b94](https://github.com/rtammekivi/hikconnect_intercom/commit/ab32b941d37f058921969d3d145e88095ab80d3d))
* **call:** merge reject into hang up and send callSignal exactly like the app ([173d7b1](https://github.com/rtammekivi/hikconnect_intercom/commit/173d7b1d92c9b1c46199b5fe347d208b15f31227))
* **call:** send answer/reject/hang-up the way the Hik-Connect app does ([05c27ea](https://github.com/rtammekivi/hikconnect_intercom/commit/05c27ea1a96486d9525bdc19d81d7ae1d45b0491))
* **call:** track answer/hang-up locally and correct last ring for the station clock ([127bf76](https://github.com/rtammekivi/hikconnect_intercom/commit/127bf7624030609f8b6975960badfd774f91d0e6))
* drop stale call polls before publishing and refresh cached calling status ([3a4131d](https://github.com/rtammekivi/hikconnect_intercom/commit/3a4131d41060a5b9d56077e47dbbe60dff3dfecd))
* ignore local call status in doorbell edge detection and request XML operation time ([0f8fb28](https://github.com/rtammekivi/hikconnect_intercom/commit/0f8fb28dd012126f2c0d7e335a2a3f1967343f17))
* keep doorbell state across failed polls and restore last ring time per message ([93b1774](https://github.com/rtammekivi/hikconnect_intercom/commit/93b17747ed4995f712b12dd37c7a02de83a7d910))
* only drop call polls older than a button press and keep the last known ring ([11d9e71](https://github.com/rtammekivi/hikconnect_intercom/commit/11d9e719c9c17fce476a445da8a549cdfea2dce9))
* **push:** fall back to the ezvizlife push host and keep MQTT alive ([01c3502](https://github.com/rtammekivi/hikconnect_intercom/commit/01c350299b81eb7acd8c77902825cb1eb6031299))
* retry last ring clock correction when the station time is unavailable ([69910e9](https://github.com/rtammekivi/hikconnect_intercom/commit/69910e90e5ac4ce124e9c8d3b3593cccc4abae0e))

## [0.10.8](https://github.com/rtammekivi/hikconnect_intercom/compare/v0.10.7...v0.10.8) (2026-10-13)


### Bug Fixes

* **camera:** decode encrypted recorder streams and release cancelled connections ([b0c17eb](https://github.com/rtammekivi/hikconnect_intercom/commit/b0c17eba4b9132ac00db307ff8d2180b80f34927))
* **camera:** support encrypted recorder video and prevent stream slot leaks ([66c278b](https://github.com/rtammekivi/hikconnect_intercom/commit/66c278b18a76bcd7a1b3261515a5169006e24604))
* **ci:** use test requirements for Python dependency cache ([6067ea7](https://github.com/rtammekivi/hikconnect_intercom/commit/6067ea7cee88630fe2146b848bb8b7981e128f2e))

## [0.10.7](https://github.com/rtammekivi/hikconnect_intercom/compare/v0.10.6...v0.10.7) (2026-10-01)


### Bug Fixes

* **manifest:** drop cryptography and requests requirements provided by Home Assistant core ([ca382e4](https://github.com/rtammekivi/hikconnect_intercom/commit/ca382e4f71fd3a7f23777d7c9886af1fe435857c))
* **manifest:** drop cryptography and requests requirements provided by Home Assistant core ([b90454d](https://github.com/rtammekivi/hikconnect_intercom/commit/b90454d99702203cc566980b27da91ac08531ba4))

## [0.10.6](https://github.com/rtammekivi/hikconnect_intercom/compare/v0.10.5...v0.10.6) (2026-09-11)


### Bug Fixes

* **cas:** anchor the legacy CAS certificate chain so non-EU regions work ([728b119](https://github.com/rtammekivi/hikconnect_intercom/commit/728b11972a9614fe65614ff00d643a88586908c3))
* **cas:** anchor the legacy CAS certificate chain so non-EU regions work ([081f58f](https://github.com/rtammekivi/hikconnect_intercom/commit/081f58fe8aeff543c4546807150d2afb4b84a8d8)), closes [#9](https://github.com/rtammekivi/hikconnect_intercom/issues/9)

## [0.10.5](https://github.com/rtammekivi/hikconnect_intercom/compare/v0.10.4...v0.10.5) (2026-08-23)


### Bug Fixes

* **api:** stop the cloud rate-limiting itself out of a session ([f8f8457](https://github.com/rtammekivi/hikconnect_intercom/commit/f8f8457525606dc68d78aa3dfa4ec988f86f906c))
* **api:** stop the cloud rate-limiting itself out of a session ([788c3ca](https://github.com/rtammekivi/hikconnect_intercom/commit/788c3ca999b25ab6fd3a1d092f3e91ba6ba25b2c))

## [0.10.4](https://github.com/rtammekivi/hikconnect_intercom/compare/v0.10.3...v0.10.4) (2026-08-07)


### Performance Improvements

* **camera:** hold the warm pipeline 10s, not 30s ([bf3f0af](https://github.com/rtammekivi/hikconnect_intercom/commit/bf3f0afcaff821a8e246da0022e5807cb6a9656d))

## [0.10.3](https://github.com/rtammekivi/hikconnect_intercom/compare/v0.10.2...v0.10.3) (2026-08-07)


### Performance Improvements

* **camera:** share one ffmpeg encoder per channel ([fb17f42](https://github.com/rtammekivi/hikconnect_intercom/commit/fb17f42df48a6536f7bd260a2a5dd4019a34cd04))

## [0.10.2](https://github.com/rtammekivi/hikconnect_intercom/compare/v0.10.1...v0.10.2) (2026-08-07)


### Bug Fixes

* **camera:** follow the station's LAN IP instead of caching it at setup ([c435ce0](https://github.com/rtammekivi/hikconnect_intercom/commit/c435ce07aaae157e040a8e41a9d1d24c6f4fe359))

## [0.10.1](https://github.com/rtammekivi/hikconnect_intercom/compare/v0.10.0...v0.10.1) (2026-08-07)


### Bug Fixes

* **camera:** drop stale MJPEG frames and evict whole GOPs ([a434d86](https://github.com/rtammekivi/hikconnect_intercom/commit/a434d860078f942216da59d7fb088c4abe30b9aa))

## [0.10.0](https://github.com/rtammekivi/hikconnect_intercom/compare/v0.9.5...v0.10.0) (2026-07-17)


### Features

* **camera:** share one CPD7 upstream per channel across all viewers ([0b56865](https://github.com/rtammekivi/hikconnect_intercom/commit/0b568652e3529d85f358962d99f3f2d7d905b626))


### Bug Fixes

* **api:** surface station rejection on call operations ([91d578d](https://github.com/rtammekivi/hikconnect_intercom/commit/91d578d93e7cfdbfa850b9b31a11442c22fcd7a7))

## [0.9.5](https://github.com/rtammekivi/hikconnect_intercom/compare/v0.9.4...v0.9.5) (2026-07-12)


### Bug Fixes

* **api:** renew the cloud session instead of silently serving no data ([165af52](https://github.com/rtammekivi/hikconnect_intercom/commit/165af52cae78519c7601d2ee50dd5cb271c1644f))

## [0.9.4](https://github.com/rtammekivi/hikconnect_intercom/compare/v0.9.3...v0.9.4) (2026-07-11)


### Bug Fixes

* **camera:** self-heal stale CPD7 control key (Result 3) ([0aff4ee](https://github.com/rtammekivi/hikconnect_intercom/commit/0aff4ee9ab4f2de8013c22f03cba2a5061122d34))

## [0.9.3](https://github.com/rtammekivi/hikconnect_intercom/compare/v0.9.2...v0.9.3) (2026-07-11)


### Bug Fixes

* set iot_class to cloud_push ([e368ad3](https://github.com/rtammekivi/hikconnect_intercom/commit/e368ad3b7c6f64679a45a54a9666911bc5798e1d))
