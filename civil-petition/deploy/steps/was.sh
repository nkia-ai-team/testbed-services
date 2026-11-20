# Tomcat 10.1 + Java 17 + WAR(설계 §2-3 WAS). OTel javaagent 와 OTEL_ 환경변수는 넣지 않는다(Q1).
verify_artifact petition.war
place_files was
ln -sf /usr/share/java/postgresql.jar /var/lib/tomcat10/lib/postgresql.jar
install -m 0644 -o tomcat -g tomcat "$BUNDLE/artifacts/petition.war" /var/lib/tomcat10/webapps/petition.war.new
mv -f /var/lib/tomcat10/webapps/petition.war.new /var/lib/tomcat10/webapps/petition.war
rm -rf /var/lib/tomcat10/webapps/petition
install -d -m 0755 /var/lib/petition-deploy
(cd "$BUNDLE/artifacts" && grep '  petition.war$' SHA256SUMS) > /var/lib/petition-deploy/war.sha256
# The same bytes as a release: the U7 war_redeploy decoy swaps the WAR for it through vm.service, so a decoy's lines are
# an injection's (설계 §6-2 운영자 경로, operator_session.py decoy_release). Root only writes here; 0644 like the WAR.
war_sum=$(cut -c1-64 /var/lib/petition-deploy/war.sha256)
install -D -m 0644 "$BUNDLE/artifacts/petition.war" "/var/lib/petition-deploy/releases/petition-${war_sum:0:12}.war"
# And harmless byte variants, so a decoy's release name is not one fixed value per VM (rv-t4u3 D3): the same entries
# with the same bytes plus one build mark (META-INF/petition-build.txt, nothing reads it). The decoy draws one of the
# names listed in decoy-releases.sha256 (sha256sum form), the deployed WAR's own first.
variants=/var/lib/petition-deploy/decoy-releases.sha256
printf '%s  petition-%s.war\n' "$war_sum" "${war_sum:0:12}" > "$variants.new"
for mark in 1 2 3; do
    tmp=$(mktemp /var/lib/petition-deploy/releases/.variant.XXXXXX)
    python3 - "$BUNDLE/artifacts/petition.war" "$tmp" "$mark" <<'WAR_VARIANT'
import sys, zipfile
source, target, mark = sys.argv[1], sys.argv[2], sys.argv[3]
with zipfile.ZipFile(source) as war, zipfile.ZipFile(target, "w") as out:
    for entry in war.infolist():
        if entry.filename == "META-INF/petition-build.txt":
            continue
        out.writestr(entry, war.read(entry.filename))
    note = zipfile.ZipInfo("META-INF/petition-build.txt", date_time=(2026, 11, 21, 0, 0, 0))
    out.writestr(note, f"build {mark}\n")
WAR_VARIANT
    sum=$(sha256sum "$tmp" | cut -c1-64)
    install -m 0644 "$tmp" "/var/lib/petition-deploy/releases/petition-${sum:0:12}.war"
    rm -f "$tmp"
    printf '%s  petition-%s.war\n' "$sum" "${sum:0:12}" >> "$variants.new"
done
mv -f "$variants.new" "$variants"
restart_enabled tomcat10
# WAR 를 펼치고 앱이 설 때까지 다시 읽는다(settle, lib.sh). 상한은 종전 고리(2초 간격 60번)와 같은 120초.
settle 120 2 "앱 health 응답 없음" curl -fsS -m 3 -o /dev/null "http://127.0.0.1:${WAS_PORT}/${APP_CONTEXT}/api/health"
log "was ok: $(cat /var/lib/petition-deploy/war.sha256)"
