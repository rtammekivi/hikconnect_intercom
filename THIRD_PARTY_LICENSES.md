# Third-party licenses

Vendored components under `custom_components/hikconnect_intercom/lib/` retain their
original licenses:

- `lib/lan_client.py`, `lib/crypto.py` — CPD7 LAN protocol (control + ECDH/
  ChaCha20 crypto), adapted from
  [Bobsilvio/ezviz_hp7](https://github.com/Bobsilvio/ezviz_hp7) and the original
  CPD7 reverse engineering by
  [albrzmr/ezviz_hp7](https://github.com/albrzmr/ezviz_hp7) (MIT).
- `lib/longlink/` — channel-99 long-link push client vendored unmodified from
  [RenierM26/pyEzvizApi](https://github.com/RenierM26/pyEzvizApi) at commit
  `0dcf5f4` (Apache License 2.0, Copyright Renier Moorcroft), except
  `exceptions.py`, which keeps only the two classes the client uses.
- `lib/cas.py`, `lib/_const.py` — CAS client vendored from
  [RenierM26/pyEzvizApi](https://github.com/RenierM26/pyEzvizApi)
  (Apache License 2.0, Copyright Renier Moorcroft). The full license text at
  <https://github.com/RenierM26/pyEzvizApi/blob/main/LICENSE.md> continues to
  apply to that code.

`lib/hik_decoder.py` (unencrypted Hik-Connect indoor-station media decoder) and
the rest of `custom_components/hikconnect_intercom/` are original to this project.
