FROM eclipse-temurin:17-jdk AS builder
ENV JAVA_TOOL_OPTIONS=-Djava.net.preferIPv4Stack=true
RUN echo 'precedence ::ffff:0:0/96 100' >> /etc/gai.conf
WORKDIR /build

COPY pom.xml ./pom.xml
COPY commerce-common ./commerce-common
COPY order-service/pom.xml ./order-service/pom.xml
COPY product-service/pom.xml ./product-service/pom.xml
COPY inventory-service/pom.xml ./inventory-service/pom.xml
COPY payment-service/pom.xml ./payment-service/pom.xml
COPY notification-service/pom.xml ./notification-service/pom.xml
COPY user-service/pom.xml ./user-service/pom.xml
COPY cart-service/pom.xml ./cart-service/pom.xml
COPY pricing-service/pom.xml ./pricing-service/pom.xml
COPY shipping-service/pom.xml ./shipping-service/pom.xml
COPY api-gateway/pom.xml ./api-gateway/pom.xml

RUN apt-get -o Acquire::ForceIPv4=true update && apt-get -o Acquire::ForceIPv4=true install -y maven && \
    mvn -pl commerce-common -am install -DskipTests -q && \
    mvn -pl payment-service dependency:go-offline -q

COPY payment-service/src ./payment-service/src
RUN mvn -pl payment-service package -DskipTests -q

FROM eclipse-temurin:17-jre
WORKDIR /app
COPY --from=builder /build/payment-service/target/*.jar app.jar
RUN mkdir -p /opt/apm
EXPOSE 8083
ENTRYPOINT ["java", "-jar", "app.jar"]
