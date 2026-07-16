FROM eclipse-temurin:17-jdk AS builder
ENV JAVA_TOOL_OPTIONS=-Djava.net.preferIPv4Stack=true
RUN echo 'precedence ::ffff:0:0/96 100' >> /etc/gai.conf
WORKDIR /build

COPY pom.xml ./pom.xml
COPY shop-common ./shop-common
COPY post-service/pom.xml ./post-service/pom.xml
COPY feed-service/pom.xml ./feed-service/pom.xml
COPY comment-service/pom.xml ./comment-service/pom.xml
COPY notification-service/pom.xml ./notification-service/pom.xml

RUN apt-get -o Acquire::ForceIPv4=true update && apt-get -o Acquire::ForceIPv4=true install -y maven && \
    mvn -pl shop-common -am install -DskipTests -q && \
    mvn -pl post-service dependency:go-offline -q

COPY post-service/src ./post-service/src
RUN mvn -pl post-service package -DskipTests -q

FROM eclipse-temurin:17-jre
WORKDIR /app
COPY --from=builder /build/post-service/target/*.jar app.jar
RUN mkdir -p /opt/apm
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "app.jar"]
