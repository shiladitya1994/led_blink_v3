pipeline {
    agent any

    stages {

        stage('Checkout') {
            steps {
                checkout scm
            }
        }

        stage('Build Firmware (Windows Native)') {
            steps {
                bat '''
                echo ===============================
                echo Loading ESP-IDF Environment
                echo ===============================

                call C:\\Espressif\\frameworks\\esp-idf-v5.5.2\\export.bat

                idf.py --version
                idf.py build
                '''
            }
        }

        stage('Archive Firmware') {
            steps {
                archiveArtifacts artifacts: 'build\\*.bin, build\\*.elf, build\\*.map', fingerprint: true
            }
        }
    }

    post {
        success {
            echo 'Build succeeded'
        }
        failure {
            echo 'Build failed'
        }
    }
}
