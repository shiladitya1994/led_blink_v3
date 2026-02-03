pipeline {
    agent any

    environment {
        ESP_IDF_IMAGE = "docker.io/espressif/idf:v5.5.2"
    }

    stages {
        stage('Checkout') {
            steps {
                checkout scm
            }
        }

        stage('ESP-IDF Build') {
            steps {
                sh '''
                    podman run --rm \
                      -v "$PWD:/project" \
                      -w /project \
                      ${ESP_IDF_IMAGE} \
                      bash -c "
                        source /opt/esp/idf/export.sh &&
                        idf.py set-target esp32 &&
                        idf.py build
                      "
                '''
            }
        }
    }

    post {
        success {
            echo "✅ ESP-IDF build succeeded"
        }
        failure {
            echo "❌ ESP-IDF build failed"
        }
    }
}
