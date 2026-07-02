#!/bin/sh
set -e

# Replace the API_URL placeholder in the nginx config with the runtime value.
sed -i "s|API_URL_PLACEHOLDER|${API_URL}|g" /etc/nginx/conf.d/default.conf

exec "$@"
