FROM eclipse-temurin:17-jdk AS builder
ENV JAVA_TOOL_OPTIONS=-Djava.net.preferIPv4Stack=true
RUN echo 'precedence ::ffff:0:0/96 100' >> /etc/gai.conf
WORKDIR /build

COPY pom.xml ./pom.xml
COPY shop-common ./shop-common
COPY api-service/pom.xml ./api-service/pom.xml
COPY account-service/pom.xml ./account-service/pom.xml
COPY transfer-service/pom.xml ./transfer-service/pom.xml
COPY ledger-service/pom.xml ./ledger-service/pom.xml

RUN apt-get -o Acquire::ForceIPv4=true update && apt-get -o Acquire::ForceIPv4=true install -y maven && \
    mvn -pl shop-common -am install -DskipTests -q && \
    mvn -pl account-service dependency:go-offline -q

COPY account-service/src ./account-service/src
RUN mvn -pl account-service package -DskipTests -q

FROM eclipse-temurin:17-jre
WORKDIR /app
COPY --from=builder /build/account-service/target/*.jar app.jar
RUN mkdir -p /opt/apm
EXPOSE 8081
ENTRYPOINT ["java", "-jar", "app.jar"]
