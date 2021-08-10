{{/* vim: set filetype=mustache: */}}
{{/*
Expand the name of the chart.
*/}}
{{- define "chart.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "chart.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "chart.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
kube-janitor arguments
*/}}
{{- define "kubeJanitor.args" -}}
{{- if .Values.kubejanitor.dryRun }}
- "--dry-run"
{{- end }}
{{- if .Values.kubejanitor.debug }}
- "--debug"
{{- end }}
{{- if .Values.kubejanitor.once }}
- "--once"
{{- end }}
{{- if .Values.kubejanitor.interval }}
- "--interval"
- "{{ .Values.kubejanitor.interval }}"
{{- end }}
{{- if .Values.kubejanitor.includeResources }}
- "--include-resources"
- "{{ .Values.kubejanitor.includeResources }}"
{{- end }}
{{- if .Values.kubejanitor.excludeResources }}
- "--exclude-resources"
- "{{ .Values.kubejanitor.excludeResources }}"
{{- end }}
{{- if .Values.kubejanitor.includeNamespaces }}
- "--include-namespaces"
- "{{ .Values.kubejanitor.includeNamespaces }}"
{{- end }}
{{- if .Values.kubejanitor.excludeNamespaces }}
- "--exclude-namespaces"
- "{{ .Values.kubejanitor.excludeNamespaces }}"
{{- end }}
{{- if .Values.kubejanitor.additionalArgs }}
{{ toYaml .Values.kubejanitor.additionalArgs }}
{{- end }}
- --rules-file=/config/rules.yaml
{{- end -}}
