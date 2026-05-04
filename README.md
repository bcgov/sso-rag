# About

This repository is a POC to use a RAG system for sso-documentation. It uses ollama and gemma as a base model.

## Helm

This is deployed with helm into openshift. Use the following commands from the helm dir to use the chart: 

- deploy: `helm install sso-rag . -n <namespace> -f values.yaml`
- upgrade: `helm upgrade sso-rag . -n <namespace> -f values.yaml`
- uninstall: `helm uninstall sso-rag . -n <namespace> -f values.yaml`

It contains a deployment with the running model and a service, but has no internet exposure. To test the service, you can port forward it to your local machine:

- `oc port-forward svc/sso-rag-ollama 11434:11434`

Then you can query it from another terminal:

``` bash
curl http://localhost:11434/api/generate -d '{
  "model": "gemma:2b",
  "prompt": "What is OIDC?",
  "stream": false
}'
```

## Hardware

Gemma 2B recommends [8GB Ram](https://github.com/google-deepmind/gemma#system-requirements). There is not a listed CPU requirement, but from usage it is very CPU thirsty, e.g. firing a request spikes it to 8 CPU usage. The image currently requests 2 CPU and has no limit set (as recommended by platform-services), so that it can handle those spikes. Under more frequent usage we would need to increase the requests to guarantee stability.