{{- define "cue.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "cue.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "cue.selector" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "cue.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}
{{- end -}}

{{- define "cue.databaseSecret" -}}
{{- if .Values.database.existingSecret -}}
{{ .Values.database.existingSecret }}
{{- else -}}
{{ include "cue.fullname" . }}-database
{{- end -}}
{{- end -}}

{{/* Environment shared by every container. */}}
{{- define "cue.env" -}}
- name: CUE_CONFIG_FILE
  value: /etc/cue/cue.toml
- name: CUE_DATABASE__URL
  valueFrom:
    secretKeyRef:
      name: {{ include "cue.databaseSecret" . }}
      key: {{ .Values.database.existingSecret | ternary .Values.database.existingSecretKey "url" }}
- name: CUE_METRICS
  value: {{ .Values.metrics.enabled | quote }}
{{- range $name, $value := .Values.env }}
- name: {{ $name }}
  value: {{ $value | quote }}
{{- end }}
{{- range $name, $ref := .Values.secretEnv }}
- name: {{ $name }}
  valueFrom:
    secretKeyRef:
      name: {{ $ref.secretName }}
      key: {{ $ref.key }}
{{- end }}
{{- end -}}

{{/* Pod spec pieces shared by the API and the worker. */}}
{{- define "cue.podCommon" -}}
securityContext:
  {{- toYaml .Values.podSecurityContext | nindent 2 }}
{{- with .Values.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.affinity }}
affinity:
  {{- toYaml . | nindent 2 }}
{{- end }}
volumes:
  - name: config
    configMap:
      name: {{ include "cue.fullname" . }}
  - name: tmp
    emptyDir: {}
{{- if .Values.migrate }}
initContainers:
  - name: migrate
    image: {{ include "cue.image" . }}
    imagePullPolicy: {{ .Values.image.pullPolicy }}
    args: ["db", "upgrade"]
    env:
      {{- include "cue.env" . | nindent 6 }}
    {{- with .Values.envFrom }}
    envFrom:
      {{- toYaml . | nindent 6 }}
    {{- end }}
    securityContext:
      {{- toYaml .Values.securityContext | nindent 6 }}
    volumeMounts:
      - {name: config, mountPath: /etc/cue, readOnly: true}
      - {name: tmp, mountPath: /tmp}
{{- end }}
{{- end -}}
