curl --url '<https://a6api.com/api/supplier/me/profile>' \
  -X 'PATCH' \
  -H 'accept: application/json, text/plain, */*' \
  -H 'accept-language: en,zh-CN;q=0.9,zh;q=0.8' \
  -H 'cache-control: no-store' \
  -H 'content-type: application/json' \
  -b 'session=SESSION_COOKIE' \
  -H 'new-api-user: A6_USER_ID' \
  -H 'origin: <https://a6api.com>' \
  -H 'priority: u=1, i' \
  -H 'referer: <https://a6api.com/supplier/profile>' \
  -H 'sec-ch-ua: "Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"' \
  -H 'sec-ch-ua-mobile: ?0' \
  -H 'sec-ch-ua-platform: "macOS"' \
  -H 'sec-fetch-dest: empty' \
  -H 'sec-fetch-mode: cors' \
  -H 'sec-fetch-site: same-origin' \
  -H 'user-agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36' \
  --data-raw '{"contact_info":"<CONTACT_INFO>","description":"稳定商户"}'

{"message":"","success":true}
